"""E2 worker-side parsers: one GDELT 15-minute file (zip bytes, in memory) -> compact labelled frames.

All functions are pure (bytes + WorkerContext -> polars frames) so they run in a process pool and are unit-tested on
recorded row fixtures. Column pruning: only the needed columns are materialised by the Polars CSV reader.

GKG 2.1 (`parse_gkg`):
  1. column-pruned read (9 of 27 columns);
  2. PREFILTER (cheap, vectorised) before the label parse: market-theme regex on V1Themes, English headline rules on
     <PAGE_TITLE>, and an Aho-Corasick scan (`str.contains_any`) of V1Organizations against the issuer alias table;
     rows passing none of the three are discarded here;
  3. label parse of the survivors: explode + normalise organisations -> exact alias join -> context-adjusted link
     confidence (headline / theme cues, app.nlp.gdelt_link rules) -> keep links >= 0.5; countries (V1Locations FIPS ->
     ISO2, ranked by mentions, top 3 + GLOBAL rule, fallback = linked issuers' countries); sector mask (GKG theme ->
     sector | issuer sectors); tone / polarity; url hash; cleaned title (or URL-slug headline).
  A row is kept iff theme_mask != 0 or it has an issuer link.
Events 2.0 (`parse_events`): every event (they all feed T1), compact ints; CAMEO -> theme mask; countries.
Mentions (`parse_mentions`): (event_id, url_hash) for web mentions (used to propagate article labels to events).
"""

from __future__ import annotations

import hashlib
import html
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any

import polars as pl

from app.events_v2 import taxonomy as tx
from app.events_v2.dicts import _MIN_LEN_WHITELIST, norm_org_expr
from app.ingestion.gdelt_events import EVENT_COLS, FIPS_TO_ISO2, ISO3_TO_ISO2, read_tsv
from app.ingestion.gdelt_gkg import GKG_COLS, LANG3_TO_2

MIN_LINK = 0.5
MENTION_COLS = ["GLOBALEVENTID", "EventTimeDate", "MentionTimeDate", "MentionType", "MentionSourceName",
                "MentionIdentifier", "SentenceID", "Actor1CharOffset", "Actor2CharOffset", "ActionCharOffset",
                "InRawText", "Confidence", "MentionDocLen", "MentionDocTone", "MentionDocTranslationInfo", "Extras"]
_GKG_USE = ["DATE", "SourceCommonName", "DocumentIdentifier", "V1Themes", "V2EnhancedThemes", "V1Locations", "V1Organizations", "V15Tone",
            "V21TranslationInfo", "V2ExtrasXML"]
_EV_USE = ["GLOBALEVENTID", "SQLDATE", "Actor1CountryCode", "Actor2CountryCode", "EventCode", "QuadClass",
           "GoldsteinScale", "NumMentions", "NumSources", "NumArticles", "AvgTone", "ActionGeo_CountryCode",
           "ActionGeo_Lat", "ActionGeo_Long", "DATEADDED", "SOURCEURL"]
_MEN_USE = ["GLOBALEVENTID", "MentionType", "MentionIdentifier", "Confidence"]

_THEME_PREFILTER = "|".join(
    re.escape(g) for t in tx.THEMES for g in (*t.gkg, *t.gkg_prefix))  # any mapped GKG theme as a substring
_I16 = 32767


def _ambig() -> frozenset[str]:
    from app.nlp.gdelt_link import AMBIGUOUS  # noqa: PLC0415

    return frozenset(k for k, v in AMBIGUOUS.items() if v < 0.9)


_AMBIG = _ambig()


@dataclass
class WorkerContext:
    """Per-process labelling context (shipped once to each pool worker)."""
    aliases: pl.DataFrame          # [org_norm, issuer_ids list[i32], conf f64, ambiguous bool]
    issuers: pl.DataFrame          # [iid i32, country str (ISO2), sector_mask i16, name str, ticker str]
    patterns: list[str]            # Aho-Corasick prefilter patterns (aliases >= 4 chars)
    short_rx: str | None           # regex for whitelisted short aliases
    names: dict[int, tuple[str, str]]  # iid -> (normalised name, ticker lower) for headline context
    legacy_dict: pl.DataFrame | None = None  # app.ingestion.gdelt_gkg dictionary (legacy gdelt_gkg outputs)

    @classmethod
    def build(cls, aliases: pl.DataFrame, issuers: pl.DataFrame) -> WorkerContext:
        keys = aliases.get_column("org_norm").to_list() if aliases.height else []
        pats = [k for k in keys if len(k) >= 4]
        short = [k for k in keys if len(k) < 4 and k in _MIN_LEN_WHITELIST]
        short_rx = (r"(?:^|;)(?:" + "|".join(re.escape(k) for k in short) + r")(?:;|$)") if short else None
        names: dict[int, tuple[str, str]] = {}
        for r in issuers.iter_rows(named=True):
            names[int(r["iid"])] = ((r.get("name_norm") or ""), (r.get("ticker") or "").lower())
        return cls(aliases, issuers, pats, short_rx, names)


# ------------------------------------------------------------------------------------------------ helpers
def unzip(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(z.namelist()[0])


def url_key_expr(col: pl.Expr) -> pl.Expr:
    """Normalised URL key (scheme / www / query / fragment / trailing slash removed, lower-case) - the same key as
    app.nlp.gdelt_link._norm_url, so v2 hashes join the legacy tables."""
    return (col.str.to_lowercase().str.replace(r"^[a-z]+://(www[0-9]*\.)?", "").str.replace(r"[?#].*$", "")
            .str.strip_chars_end("/"))


def hash64(s: str | None) -> int | None:
    if not s:
        return None
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8", "ignore"), digest_size=8).digest(), "little", signed=True)


def hash_series(keys: pl.Series) -> pl.Series:
    return pl.Series(keys.name, [hash64(k) for k in keys.to_list()], dtype=pl.Int64)


def clean_title(t: str | None) -> str | None:
    if not t:
        return None
    t = html.unescape(html.unescape(t)).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:300] or None


def _clip16(e: pl.Expr) -> pl.Expr:
    return e.clip(-_I16, _I16).cast(pl.Int16)


def _country_ids(iso: pl.Expr) -> pl.Expr:
    return iso.replace_strict(tx.COUNTRY_ID, default=None, return_dtype=pl.Int16)


# ------------------------------------------------------------------------------------------------ GKG
_EMPTY_ART = {
    "date": pl.Date, "minute": pl.Int16, "url_hash": pl.Int64, "url": pl.Utf8, "domain": pl.Utf8, "lang": pl.Utf8,
    "tone_x100": pl.Int16, "polarity_x10": pl.Int16, "theme_mask": pl.Int64, "sector_mask": pl.Int16,
    "countries": pl.List(pl.Int16), "issuer_ids": pl.List(pl.Int32), "issuer_conf": pl.List(pl.Int8),
    "title": pl.Utf8, "translated": pl.Boolean,
}


def empty_articles() -> pl.DataFrame:
    return pl.DataFrame(schema=_EMPTY_ART)


_BIZ_RX = re.compile(r"(?:^|;)(?:ECON_|EPU_ECONOMY|EPU_CATS_|WB_1920_|WB_1921_|SOC_INNOVATION|SOC_EMERGINGTECH|"
                     r"TAX_FNCACT_(?:CEO|EXECUTIVE|CHIEF_EXECUTIVE|INVESTORS?|SHAREHOLDERS?|ANALYSTS?|CHAIRMAN|FOUNDER))")


def final_conf(org: str, base: float, title: str | None, themes: str | None, iids: list[int], translated: bool,
               ctx: WorkerContext, ambiguous: bool = False) -> float:
    """Context-adjusted link confidence (headline / theme cues from app.nlp.gdelt_link + business-context rule).

    * contradicting headline / theme cue -> 0.05;
    * headline confirms (issuer full name, $TICKER / (TICKER), the alias itself when >= 6 chars or multi-word, a brand
      cue) -> >= 0.9;
    * otherwise a link needs business context (economic / corporate GKG themes) unless it is a strong multi-word
      alias; translated articles (no usable English headline) always need it. Unconfirmed links drop below 0.5."""
    from app.nlp.gdelt_link import BRAND_CUES, CONTRA_CUES, CONTRA_THEMES, FINANCE_CUES  # noqa: PLC0415

    t = (title or "").lower()
    contra = CONTRA_CUES.get(org)
    if (contra and contra.search(t)) or any(c in (themes or "") for c in CONTRA_THEMES.get(org, ())):
        return 0.05
    biz = bool(_BIZ_RX.search(themes or ""))
    multi = " " in org
    if not translated and t:
        tn = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)) + " "
        for iid in iids:
            name, tick = ctx.names.get(iid, ("", ""))
            if name and name != org and len(name) > len(org) + 3 and f" {name} " in tn:
                return max(base, 0.92)
            if tick and len(tick) >= 2 and re.search(rf"(\$|\(|nyse:\s*|nasdaq:\s*){re.escape(tick)}\b", t):
                return max(base, 0.92)
        cues = BRAND_CUES.get(org)
        if (len(org) >= 6 or multi) and f" {org} " in tn:
            return max(base, 0.9)
        if cues and cues.search(t):
            return max(base, 0.9)
        if f" {org} " in tn and (FINANCE_CUES.search(t) or biz):
            return max(base, 0.75)
    if ambiguous or org in _AMBIG:
        return min(base, 0.45)
    if translated:
        return base if biz and base >= 0.6 else min(base, 0.45)
    if (multi and base >= 0.9) or biz:
        return base
    return min(base, 0.45)


def link_orgs(pre: pl.DataFrame, ctx: WorkerContext, translated: bool = False) -> pl.DataFrame:
    """[__r, issuer_ids list[i32], issuer_conf list[i8]] for prefiltered rows (row index __r)."""
    empty = pl.DataFrame(schema={"__r": pl.UInt32, "issuer_ids": pl.List(pl.Int32), "issuer_conf": pl.List(pl.Int8)})
    if pre.is_empty() or ctx.aliases.is_empty():
        return empty
    ex = (pre.select("__r", pl.col("V1Organizations").fill_null("").str.split(";").alias("org"))
          .explode("org").filter(pl.col("org").str.len_chars() > 1)
          .with_columns(norm_org_expr(pl.col("org")).alias("org_norm"))
          .unique(["__r", "org_norm"]).join(ctx.aliases, on="org_norm", how="inner"))
    if ex.is_empty():
        return empty
    ctxd = ex.join(pre.select("__r", "__title", "V1Themes"), on="__r", how="left")
    adj = [final_conf(o, c, t, th, list(ii), translated, ctx, bool(a)) for o, c, t, th, ii, a in
           ctxd.select("org_norm", "conf", "__title", "V1Themes", "issuer_ids", "ambiguous").iter_rows()]
    ex = ctxd.with_columns(pl.Series("conf", adj, pl.Float64)).select(ex.columns)
    ex = ex.filter(pl.col("conf") >= MIN_LINK).explode("issuer_ids").rename({"issuer_ids": "iid"})
    g = ex.group_by("__r", "iid").agg(pl.col("conf").max()).sort("__r", "conf", descending=[False, True])
    return g.group_by("__r", maintain_order=True).agg(
        pl.col("iid").head(10).cast(pl.Int32).alias("issuer_ids"),
        (pl.col("conf").head(10) * 100).round().cast(pl.Int8).alias("issuer_conf"))


def parse_gkg(raw: bytes, ctx: WorkerContext, translated: bool = False) -> tuple[pl.DataFrame, dict[str, int]]:
    """One GKG file (unzipped bytes) -> (kept article rows, stats)."""
    idx = [GKG_COLS.index(c) for c in _GKG_USE]
    df = read_tsv(raw, GKG_COLS, idx)
    n = df.height
    if not n:
        return empty_articles(), {"rows": 0, "prefiltered": 0, "kept": 0}
    df = df.with_row_index("__r").with_columns(
        tx.gkg_theme_mask_counted_expr("V2EnhancedThemes", 3 if translated else tx.BROAD_MIN_MENTIONS).alias("__tm"),
        pl.col("V2ExtrasXML").str.extract(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", 1).str.strip_chars().alias("__title"))
    df = df.with_columns(
        (tx.title_theme_mask_expr("__title") if not translated else pl.lit(0, pl.Int64)).alias("__ttm"))
    orgs = pl.col("V1Organizations").fill_null("").str.to_lowercase()
    cand = orgs.str.contains_any(ctx.patterns, ascii_case_insensitive=True) if ctx.patterns else pl.lit(False)
    if ctx.short_rx:
        cand = cand | orgs.str.contains(ctx.short_rx)
    pre = df.filter((pl.col("__tm") != 0) | (pl.col("__ttm") != 0) | cand)
    n_pre = pre.height
    links = link_orgs(pre.filter(cand), ctx, translated)
    pre = pre.join(links, on="__r", how="left").with_columns(
        (pl.col("__tm") | pl.col("__ttm")).alias("theme_mask"),
        pl.col("issuer_ids").fill_null(pl.lit([], pl.List(pl.Int32))),
        pl.col("issuer_conf").fill_null(pl.lit([], pl.List(pl.Int8))))
    kept = pre.filter((pl.col("theme_mask") != 0) | (pl.col("issuer_ids").list.len() > 0))
    if kept.is_empty():
        return empty_articles(), {"rows": n, "prefiltered": n_pre, "kept": 0}
    # ---- countries: V1Locations 'type#name#FIPS#adm1#lat#lon#feature;...' ranked by mentions
    loc = (kept.select("__r", pl.col("V1Locations").fill_null("").str.split(";").alias("l")).explode("l")
           .with_columns(pl.col("l").str.split("#").list.get(2, null_on_oob=True)
                         .replace_strict(FIPS_TO_ISO2, default=None, return_dtype=pl.Utf8).alias("iso"))
           .filter(pl.col("iso").is_not_null()).group_by("__r", "iso").len()
           .sort(["__r", "len", "iso"], descending=[False, True, False])
           .group_by("__r", maintain_order=True).agg(pl.col("iso").alias("__iso"), pl.col("len").alias("__n")))
    kept = kept.join(loc, on="__r", how="left")
    # issuer-derived countries / sectors
    im = ctx.issuers.select(pl.col("iid").cast(pl.Int32), pl.col("country"), pl.col("sector_mask").cast(pl.Int16))
    iss = (kept.select("__r", "issuer_ids").explode("issuer_ids").drop_nulls()
           .join(im, left_on="issuer_ids", right_on="iid", how="inner")
           .group_by("__r").agg(pl.col("country").drop_nulls().unique(maintain_order=True).alias("__icty"),
                                pl.col("sector_mask").fill_null(0).cast(pl.Int32).alias("__ism")))
    kept = kept.join(iss, on="__r", how="left")
    tone = pl.col("V15Tone").str.split(",")
    srclc = pl.col("V21TranslationInfo").str.extract(r"srclc:([a-z]{3})", 1)
    out = kept.select(
        pl.col("DATE").str.slice(0, 8).str.to_date("%Y%m%d", strict=False).alias("date"),
        (pl.col("DATE").str.slice(8, 2).cast(pl.Int16, strict=False) * 60
         + pl.col("DATE").str.slice(10, 2).cast(pl.Int16, strict=False)).cast(pl.Int16).alias("minute"),
        pl.col("DocumentIdentifier").alias("url"),
        url_key_expr(pl.col("DocumentIdentifier")).alias("__key"),
        pl.col("SourceCommonName").str.to_lowercase().str.replace(r"^www\.", "").alias("domain"),
        (srclc.replace_strict(LANG3_TO_2, default=None, return_dtype=pl.Utf8) if translated else pl.lit("en")).alias("lang"),
        _clip16((tone.list.get(0, null_on_oob=True).cast(pl.Float64, strict=False) * 100).round()).alias("tone_x100"),
        _clip16((tone.list.get(3, null_on_oob=True).cast(pl.Float64, strict=False) * 10).round()).alias("polarity_x10"),
        "theme_mask",
        "__iso", "__n", "__icty", "issuer_ids", "issuer_conf", "__title",
        pl.lit(translated).alias("translated"),
    )
    # sector mask = sector-specific market themes (taxonomy.SECTOR_LABEL_THEMES) | OR of the linked issuers' sectors
    sm = []
    for base, isl in zip(kept.select(tx.theme_sector_mask_expr("theme_mask")).to_series().to_list(),
                         kept.get_column("__ism").to_list(), strict=True):
        m = int(base or 0)
        for x in isl or []:
            m |= int(x or 0)
        sm.append(m)
    out = out.with_columns(pl.Series("sector_mask", sm, pl.Int16))
    # GLOBAL rule + id mapping (python: small frames)
    ctys: list[list[int]] = []
    for iso, cnt, icty, tm in out.select("__iso", "__n", "__icty", "theme_mask").iter_rows():
        if iso:
            labs = tx.countries_with_global(list(zip(iso, cnt, strict=True)), int(tm or 0))
        elif icty:
            labs = list(icty[: tx.MAX_COUNTRIES])
        else:
            labs = [tx.GLOBAL]
        ctys.append([tx.COUNTRY_ID[c] for c in labs if c in tx.COUNTRY_ID] or [tx.GLOBAL_ID])
    titles = [clean_title(t) for t in out.get_column("__title").to_list()]
    out = out.with_columns(pl.Series("countries", ctys, pl.List(pl.Int16)), pl.Series("title", titles, pl.Utf8),
                           hash_series(out.get_column("__key")).alias("url_hash"))
    out = out.filter(pl.col("date").is_not_null() & pl.col("url_hash").is_not_null())
    out = out.unique("url_hash", keep="first").select(list(_EMPTY_ART))
    return out, {"rows": n, "prefiltered": n_pre, "kept": out.height,
                 "linked": int(out.filter(pl.col("issuer_ids").list.len() > 0).height)}


# ------------------------------------------------------------------------------------------------ Events 2.0
_EMPTY_EV = {
    "event_id": pl.Int64, "date": pl.Date, "event_date": pl.Date, "cameo": pl.Int16, "cameo_root": pl.Int8,
    "quad": pl.Int8, "goldstein_x10": pl.Int8, "tone_x100": pl.Int16, "n_mentions": pl.Int16, "n_sources": pl.Int16,
    "n_articles": pl.Int16, "country": pl.Int16, "countries": pl.List(pl.Int16), "lat_x100": pl.Int16,
    "lon_x100": pl.Int16, "url_hash": pl.Int64, "url": pl.Utf8, "domain": pl.Utf8, "theme_mask": pl.Int64,
    "translated": pl.Boolean,
}


def empty_events() -> pl.DataFrame:
    return pl.DataFrame(schema=_EMPTY_EV)


def parse_events(raw: bytes, translated: bool = False) -> pl.DataFrame:
    """One export file -> every event (compact). The T2 keep filter runs later (needs article links)."""
    idx = [EVENT_COLS.index(c) for c in _EV_USE]
    df = read_tsv(raw, EVENT_COLS, idx)
    if df.is_empty():
        return empty_events()
    act = pl.col("ActionGeo_CountryCode").replace_strict(FIPS_TO_ISO2, default=None, return_dtype=pl.Utf8)
    a1 = pl.col("Actor1CountryCode").replace_strict(ISO3_TO_ISO2, default=None, return_dtype=pl.Utf8)
    a2 = pl.col("Actor2CountryCode").replace_strict(ISO3_TO_ISO2, default=None, return_dtype=pl.Utf8)
    code = pl.col("EventCode").fill_null("")
    df = df.with_columns(
        pl.col("GLOBALEVENTID").cast(pl.Int64, strict=False).alias("event_id"),
        pl.col("DATEADDED").str.slice(0, 8).str.to_date("%Y%m%d", strict=False).alias("date"),
        pl.col("SQLDATE").str.to_date("%Y%m%d", strict=False).alias("event_date"),
        code.cast(pl.Int16, strict=False).alias("cameo"),
        code.str.slice(0, 2).cast(pl.Int8, strict=False).alias("cameo_root"),
        pl.col("QuadClass").cast(pl.Int8, strict=False).fill_null(0).alias("quad"),
        (pl.col("GoldsteinScale").cast(pl.Float64, strict=False) * 10).round().clip(-100, 100).cast(pl.Int8).alias("goldstein_x10"),
        _clip16((pl.col("AvgTone").cast(pl.Float64, strict=False) * 100).round()).alias("tone_x100"),
        *[_clip16(pl.col(c).cast(pl.Int64, strict=False).fill_null(0)).alias(o) for c, o in
          (("NumMentions", "n_mentions"), ("NumSources", "n_sources"), ("NumArticles", "n_articles"))],
        _clip16((pl.col("ActionGeo_Lat").cast(pl.Float64, strict=False) * 100).round()).alias("lat_x100"),
        _clip16((pl.col("ActionGeo_Long").cast(pl.Float64, strict=False) * 100).round()).alias("lon_x100"),
        pl.concat_list([act, a1, a2]).list.drop_nulls().list.unique(maintain_order=True).alias("__c"),
        pl.col("SOURCEURL").alias("url"),
        url_key_expr(pl.col("SOURCEURL")).alias("__key"),
        pl.col("SOURCEURL").str.extract(r"^[a-zA-Z]+://(?:www[0-9]*\.)?([^/:?#]+)", 1).str.to_lowercase().alias("domain"),
        tx.cameo_theme_mask_expr("EventCode").alias("theme_mask"),
        pl.lit(translated).alias("translated"),
    ).filter(pl.col("event_id").is_not_null() & pl.col("date").is_not_null() & pl.col("cameo").is_not_null()
             & pl.col("cameo_root").is_not_null())
    cid = pl.col("__c").list.eval(pl.element().replace_strict(tx.COUNTRY_ID, default=None, return_dtype=pl.Int16)) \
        .list.drop_nulls()
    df = df.with_columns(pl.when(cid.list.len() > 0).then(cid).otherwise(pl.lit([tx.GLOBAL_ID], pl.List(pl.Int16)))
                         .alias("countries"))
    df = df.with_columns(pl.col("countries").list.first().cast(pl.Int16).alias("country"),
                         hash_series(df.get_column("__key")).alias("url_hash"))
    return df.select(list(_EMPTY_EV))


# ------------------------------------------------------------------------------------------------ Mentions
def parse_mentions(raw: bytes) -> pl.DataFrame:
    """Web mentions -> [event_id, url_hash] (distinct)."""
    idx = [MENTION_COLS.index(c) for c in _MEN_USE]
    df = read_tsv(raw, MENTION_COLS, idx)
    if df.is_empty():
        return pl.DataFrame(schema={"event_id": pl.Int64, "url_hash": pl.Int64})
    df = df.filter(pl.col("MentionType") == "1").select(
        pl.col("GLOBALEVENTID").cast(pl.Int64, strict=False).alias("event_id"),
        url_key_expr(pl.col("MentionIdentifier")).alias("__key")).drop_nulls().unique()
    return df.with_columns(hash_series(df.get_column("__key")).alias("url_hash")).select("event_id", "url_hash")


# ------------------------------------------------------------------------------------------------ dispatch
_CTX: WorkerContext | None = None


def init_worker(ctx_path: str) -> None:
    """ProcessPool initializer: load the labelling context once per worker process."""
    global _CTX  # noqa: PLW0603
    import pickle  # noqa: PLC0415

    with open(ctx_path, "rb") as fh:
        _CTX = pickle.load(fh)  # noqa: S301 - our own file


def parse_file(kind: str, data: bytes, legacy: bool = False) -> tuple[str, Any, dict[str, Any], Any]:
    """Worker entry: kind in export | mentions | gkg | translation.export | translation.mentions | translation.gkg.

    legacy=True also runs the pre-v2 parsers on the same bytes (nightly compatibility: gdelt_events table,
    country_daily files, GKG org mentions + news_items), so the old outputs need no second download."""
    import time  # noqa: PLC0415

    t0 = time.perf_counter()
    raw = unzip(data)
    del data
    translated = kind.startswith("translation.")
    base = kind.split(".")[-1]
    stats: dict[str, Any] = {"raw_bytes": len(raw)}
    old: Any = None
    if base == "gkg":
        assert _CTX is not None, "worker not initialised"
        res, st = parse_gkg(raw, _CTX, translated)
        stats.update(st)
        if legacy and _CTX.legacy_dict is not None and _CTX.legacy_dict.height:
            from app.ingestion.gdelt_gkg import parse_gkg as old_gkg  # noqa: PLC0415

            old = old_gkg(raw, _CTX.legacy_dict, translated)
    elif base == "export":
        res = parse_events(raw, translated)
        stats["rows"] = res.height
        if legacy and not translated:
            from app.ingestion.gdelt_events import parse_events as old_events  # noqa: PLC0415

            old = old_events(raw)
    elif base == "mentions":
        res = parse_mentions(raw)
        stats["rows"] = res.height
    else:  # pragma: no cover
        raise ValueError(kind)
    stats["cpu_s"] = time.perf_counter() - t0
    stats["peak_rss_mb"] = peak_rss_mb()
    return kind, res, stats, old


def peak_rss_mb() -> float:
    """Peak working set of this process (MB); Windows via K32GetProcessMemoryInfo, POSIX via resource."""
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        fn = k32.K32GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if not fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return 0.0
        return round(pmc.PeakWorkingSetSize / 1e6, 1)
    except Exception:  # noqa: BLE001
        try:
            import resource  # noqa: PLC0415

            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3, 1)
        except Exception:  # noqa: BLE001
            return 0.0
