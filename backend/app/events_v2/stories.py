"""E4 story layer: near-duplicate clustering of news titles from all sources -> news_stories (T3) + story_members.

* Title cleaning for hashing: html-unescape, drop a trailing ' - Publisher' / ' | Site' segment (<= 5 words), lower,
  unicode word tokens; CJK / Thai runs become character bigrams (no spaces in those scripts).
* Near-duplicates = token-set Jaccard >= 0.6 on the cleaned title (unigrams, stop words removed). Candidates come from
  MinHash-LSH (24 permutations, 8 bands x 3 rows: P(candidate | J=0.6) ~ 0.86, J=0.8 ~ 1.0, J=0.3 ~ 0.2), verified
  exactly, then union-find.
  Exact normalised-title duplicates are merged first. A 64-bit SimHash of the canonical title is stored as a compact
  fingerprint (SimHash alone proved too sensitive on 6-10 word headlines: one changed word flips 9-12 bits).
* Window: clusters are built per UTC day; a day-cluster joins an existing story of the previous / next 3 days (72 h)
  when their canonical titles have Jaccard >= 0.6 -> the story keeps its id (story-day rows share story_id).
  New ids are deterministic (hash of day + smallest member url_hash), so re-processing a day is idempotent.
* Cross-language: GDELT's translingual GKG keeps PAGE_TITLE in the source language, so titles only cluster within a
  language (or when the publisher already wrote an English title). Measured / documented in the E5 report.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from functools import lru_cache

import numpy as np
import polars as pl

from app.events_v2 import taxonomy as tx
from app.events_v2.parse import hash64

MAX_HAMMING = 3
WINDOW_DAYS = 3
_TRAIL_RX = re.compile(r"\s+(?:[-|–—:]|·)\s+[^-|–—]{1,60}$")
_CJK_RX = re.compile(r"[぀-ヿ㐀-鿿가-힯฀-๿]+")
_WORD_RX = re.compile(r"\w+", re.U)
_STOP = frozenset("a an and as at by for from in into of on or the to with is are was were be has have says said "
                  "after over new will its it this that".split())


def hash_title(title: str | None) -> str:
    """Normalised title used for exact-duplicate grouping and hashing."""
    if not title:
        return ""
    t = html.unescape(title).strip()
    for _ in range(2):
        m = _TRAIL_RX.search(t)
        if m and len(t[: m.start()].split()) >= 4 and len(m.group(0).split()) <= 6:
            t = t[: m.start()]
    return re.sub(r"\s+", " ", t.lower()).strip()


def features(norm: str) -> list[str]:
    toks: list[str] = []
    for w in _WORD_RX.findall(norm):
        if _CJK_RX.fullmatch(w):
            toks += [w[i:i + 2] for i in range(max(1, len(w) - 1))]
        elif w not in _STOP and len(w) > 1:
            toks.append(w)
    return toks + [a + " " + b for a, b in zip(toks, toks[1:], strict=False)]


@lru_cache(maxsize=500_000)
def _fh(f: str) -> int:
    return int.from_bytes(hashlib.blake2b(f.encode("utf-8"), digest_size=8).digest(), "little")


def simhash(norm: str) -> int:
    """Signed int64 SimHash of a normalised title (0 for empty)."""
    return simhash_many([norm])[0]


_SHIFTS = np.arange(64, dtype=np.uint64)


def simhash_many(norms: list[str], chunk: int = 200_000) -> list[int]:
    """Vectorised SimHash for many normalised titles (signed int64 each; 0 for titles without features)."""
    idx: list[int] = []
    hs: list[int] = []
    for i, n in enumerate(norms):
        for f in features(n):
            idx.append(i)
            hs.append(_fh(f))
    n = len(norms)
    if not hs:
        return [0] * n
    rows = np.asarray(idx, dtype=np.int64)
    hv = np.asarray(hs, dtype=np.uint64)
    acc = np.zeros((n, 64), dtype=np.int32)
    for s in range(0, len(hv), chunk):
        h, r = hv[s:s + chunk], rows[s:s + chunk]
        bits = ((h[:, None] >> _SHIFTS) & np.uint64(1)).astype(np.int32) * 2 - 1
        starts = np.flatnonzero(np.r_[True, r[1:] != r[:-1]])
        acc[r[starts]] += np.add.reduceat(bits, starts, axis=0)
    packed = ((acc > 0).astype(np.uint64) << _SHIFTS).sum(axis=1, dtype=np.uint64)
    has = np.zeros(n, dtype=bool)
    has[rows] = True
    out = packed.view(np.int64)
    return [int(out[i]) if has[i] else 0 for i in range(n)]


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def cluster_hashes(hashes: list[int], keys: list[str] | None = None, max_bucket: int = 400) -> list[int]:
    """Cluster label (root index) per item: exact key duplicates + SimHash within MAX_HAMMING (4-band LSH)."""
    n = len(hashes)
    uf = _UF(n)
    if keys is not None:
        first: dict[str, int] = {}
        for i, k in enumerate(keys):
            if not k:
                continue
            j = first.setdefault(k, i)
            if j != i:
                uf.union(i, j)
    for band in range(4):
        buckets: dict[int, list[int]] = defaultdict(list)
        for i, h in enumerate(hashes):
            if h:
                buckets[(h >> (16 * band)) & 0xFFFF].append(i)
        for idx in buckets.values():
            if len(idx) < 2:
                continue
            idx = idx[:max_bucket]
            uniq: dict[int, int] = {}
            for i in idx:
                uniq.setdefault(hashes[i], i)
            items = list(uniq.items())
            for a in range(len(items)):
                ha, ia = items[a]
                for b in range(a + 1, len(items)):
                    hb, ib = items[b]
                    if hamming(ha, hb) <= MAX_HAMMING:
                        uf.union(ia, ib)
            for i in idx:
                uf.union(i, uniq[hashes[i]])
    return [uf.find(i) for i in range(n)]


MIN_JACCARD = 0.6
N_PERM = 24
N_BANDS = 8
_RNG = np.random.default_rng(20261012)
_PA = (_RNG.integers(1, 2**62, N_PERM, dtype=np.uint64) | np.uint64(1))
_PB = _RNG.integers(0, 2**62, N_PERM, dtype=np.uint64)


def token_set(norm: str) -> frozenset[str]:
    """Unigram token set (stop words removed; CJK / Thai runs -> character bigrams) used for Jaccard similarity."""
    return frozenset(f for f in features(norm) if " " not in f)


def minhash_signatures(sets: list[frozenset[str]]) -> np.ndarray:
    """(n, N_PERM) uint64 MinHash signatures (all-ones for empty sets)."""
    n = len(sets)
    rows: list[int] = []
    hs: list[int] = []
    for i, st in enumerate(sets):
        for t in st:
            rows.append(i)
            hs.append(_fh(t))
    sig = np.full((n, N_PERM), np.iinfo(np.uint64).max, dtype=np.uint64)
    if not hs:
        return sig
    r = np.asarray(rows, dtype=np.int64)
    h = np.asarray(hs, dtype=np.uint64)
    with np.errstate(over="ignore"):
        perm = h[:, None] * _PA[None, :] + _PB[None, :]
    starts = np.flatnonzero(np.r_[True, r[1:] != r[:-1]])
    sig[r[starts]] = np.minimum.reduceat(perm, starts, axis=0)
    return sig


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def lsh_pairs(sig: np.ndarray, max_bucket: int = 300) -> set[tuple[int, int]]:
    """Candidate pairs sharing at least one band (N_BANDS bands of N_PERM / N_BANDS rows)."""
    r = N_PERM // N_BANDS
    out: set[tuple[int, int]] = set()
    empty = np.iinfo(np.uint64).max
    for b in range(N_BANDS):
        band = np.ascontiguousarray(sig[:, b * r:(b + 1) * r])
        keys = band.view(np.dtype((np.void, band.dtype.itemsize * r))).ravel()
        buckets: dict[bytes, list[int]] = defaultdict(list)
        for i in range(sig.shape[0]):
            if band[i, 0] == empty:
                continue
            buckets[keys[i].tobytes()].append(i)
        for idx in buckets.values():
            if len(idx) < 2:
                continue
            idx = idx[:max_bucket]
            for x in range(len(idx)):
                for y in range(x + 1, len(idx)):
                    out.add((idx[x], idx[y]))
    return out


def cluster_titles(norms: list[str], min_jaccard: float = MIN_JACCARD) -> list[int]:
    """Cluster label per title: exact normalised duplicates + MinHash-LSH candidates verified by Jaccard >= min."""
    n = len(norms)
    uf = _UF(n)
    first: dict[str, int] = {}
    reps: list[int] = []
    for i, k in enumerate(norms):
        if not k:
            continue
        j = first.setdefault(k, i)
        if j != i:
            uf.union(i, j)
        else:
            reps.append(i)
    sets = [token_set(norms[i]) for i in reps]
    sig = minhash_signatures(sets)
    for a, b in lsh_pairs(sig):
        if jaccard(sets[a], sets[b]) >= min_jaccard:
            uf.union(reps[a], reps[b])
    return [uf.find(i) for i in range(n)]


# ------------------------------------------------------------------------------------------------ story assembly
MEMBER_SCHEMA = {
    "url_hash": pl.Int64, "url": pl.Utf8, "title": pl.Utf8, "minute": pl.Int16, "domain_id": pl.Int32,
    "lang_id": pl.Int8, "source_id": pl.Int8, "tone_x100": pl.Int16, "theme_mask": pl.Int64, "sector_mask": pl.Int16,
    "countries": pl.List(pl.Int16), "issuer_ids": pl.List(pl.Int32), "issuer_conf": pl.List(pl.Int8),
    "n_events": pl.Int16,
}


def _countries(lists: list[list[int] | None]) -> list[int]:
    c: Counter[int] = Counter()
    n_glob = 0
    for lst in lists:
        for x in lst or []:
            if x == tx.GLOBAL_ID:
                n_glob += 1
            else:
                c[x] += 1
    top = [k for k, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[: tx.MAX_COUNTRIES]]
    if not top or n_glob >= 0.3 * max(1, len(lists)):
        top.append(tx.GLOBAL_ID)
    return top


def _issuer_rel(rows: list[tuple[list[int] | None, list[int] | None]], title_norm: str,
                names: dict[int, tuple[str, str]]) -> tuple[list[int], list[int]]:
    n = max(1, len(rows))
    best: dict[int, int] = {}
    cnt: Counter[int] = Counter()
    for ids, conf in rows:
        for i, c in zip(ids or [], conf or [], strict=False):
            cnt[i] += 1
            best[i] = max(best.get(i, 0), int(c or 0))
    out = []
    tn = " " + re.sub(r"[^a-z0-9 ]", " ", title_norm) + " "
    for i, c in best.items():
        rel = c * math.sqrt(cnt[i] / n)
        name, tick = names.get(i, ("", ""))
        if (name and f" {name} " in tn) or (tick and len(tick) >= 2 and f" {tick} " in tn and len(tick) >= 3):
            rel = 100
        out.append((i, int(round(min(100, rel)))))
    out.sort(key=lambda x: -x[1])
    out = out[:10]
    return [i for i, _ in out], [r for _, r in out]


def salience(n_articles: int, n_sources: int, n_events: int, tone_x100: float | None, max_rel: int) -> float:
    return (math.log1p(n_articles) + math.log1p(n_sources) + 0.5 * math.log1p(n_events)
            + abs((tone_x100 or 0) / 100.0) / 5.0 + (1.0 if max_rel >= 80 else 0.5 if max_rel > 0 else 0.0))


STORY_SCHEMA = {
    "date": pl.Date, "story_id": pl.Int64, "title": pl.Utf8, "title_hash": pl.Int64, "lang_id": pl.Int8,
    "first_ts": pl.Datetime("us", "UTC"), "last_ts": pl.Datetime("us", "UTC"), "n_articles": pl.Int32,
    "n_sources": pl.Int16, "source_mask": pl.Int16, "domain_ids": pl.List(pl.Int32), "urls": pl.List(pl.Utf8),
    "tone_x100": pl.Int16, "sentiment_x100": pl.Int8, "sentiment_model": pl.Utf8, "theme_mask": pl.Int64,
    "sector_mask": pl.Int16, "countries": pl.List(pl.Int16), "issuer_ids": pl.List(pl.Int32),
    "issuer_rel": pl.List(pl.Int8), "n_events": pl.Int16, "salience": pl.Float32}


def salience_expr() -> pl.Expr:
    mr = pl.col("issuer_rel").list.max().fill_null(0)
    return (pl.col("n_articles").cast(pl.Float64).log1p() + pl.col("n_sources").cast(pl.Float64).log1p()
            + 0.5 * pl.col("n_events").cast(pl.Float64).log1p()
            + pl.col("tone_x100").cast(pl.Float64).abs().fill_null(0) / 500
            + pl.when(mr >= 80).then(1.0).when(mr > 0).then(0.5).otherwise(0.0)).cast(pl.Float32)


def link_neighbours(titles: list[str], neighbours: pl.DataFrame | None, min_articles: int = 2) -> list[int | None]:
    """For each canonical title: id of the biggest neighbour story (+-3 days) with Jaccard >= MIN_JACCARD, else None.
    min_articles: neighbours need at least this many articles (2 for day-level continuity; the hourly live fold
    passes 1 so the day's earlier singletons can grow)."""
    out: list[int | None] = [None] * len(titles)
    if neighbours is None or neighbours.is_empty() or not titles:
        return out
    nb = neighbours.sort("n_articles", descending=True).group_by("story_id", maintain_order=True).agg(
        pl.col("title").first(), pl.col("n_articles").sum())
    nb = nb.filter(pl.col("n_articles") >= min_articles)  # continuity for real stories; singletons rarely continue
    if nb.is_empty():
        return out
    nb_sets = [token_set(hash_title(t)) for t in nb.get_column("title").to_list()]
    nb_id, nb_n = nb.get_column("story_id").to_list(), nb.get_column("n_articles").to_list()
    rr = N_PERM // N_BANDS
    buckets: list[dict[bytes, list[int]]] = [defaultdict(list) for _ in range(N_BANDS)]
    nsig = minhash_signatures(nb_sets)
    for k in range(len(nb_sets)):
        if nb_sets[k]:
            for b in range(N_BANDS):
                buckets[b][nsig[k, b * rr:(b + 1) * rr].tobytes()].append(k)
    sets = [token_set(hash_title(t)) for t in titles]
    sig = minhash_signatures(sets)
    for i, ts_ in enumerate(sets):
        if not ts_:
            continue
        cands = {k for b in range(N_BANDS) for k in buckets[b].get(sig[i, b * rr:(b + 1) * rr].tobytes(), [])}
        best_n = -1
        for k in cands:
            if nb_n[k] > best_n and jaccard(ts_, nb_sets[k]) >= MIN_JACCARD:
                out[i], best_n = nb_id[k], nb_n[k]
    return out


def build_day_stories(d: date, members: pl.DataFrame, neighbours: pl.DataFrame | None = None,
                      names: dict[int, tuple[str, str]] | None = None, fixed_col: str | None = None,
                      min_neighbour_articles: int = 2) -> tuple[pl.DataFrame, pl.DataFrame]:
    """members (MEMBER_SCHEMA rows of one UTC day, titled) -> (news_stories rows, story_members rows). Vectorised.
    fixed_col: members already carry their story id in this column (roadmap v3 live fold: ids assigned by the news
    desk) -> no clustering / neighbour linking, the column is the story id."""
    names = names or {}
    m = members.filter(pl.col("title").is_not_null() & (pl.col("title").str.len_chars() > 0))
    if m.is_empty():
        return pl.DataFrame(schema=STORY_SCHEMA), pl.DataFrame()
    m = m.sort("minute", "url_hash")
    if fixed_col is not None:
        m = m.with_columns(pl.col(fixed_col).cast(pl.Int64).alias("__c"))
    else:
        norms = [hash_title(t) for t in m.get_column("title").to_list()]
        m = m.with_columns(pl.Series("__c", cluster_titles(norms), pl.Int64))
    # canonical title = most frequent member title (earliest on ties)
    canon = (m.with_row_index("__i").group_by("__c", "title").agg(pl.len().alias("__n"), pl.col("__i").min())
             .sort(["__c", "__n", "__i"], descending=[False, True, False]).unique("__c", keep="first")
             .select("__c", "title"))
    t0 = datetime(d.year, d.month, d.day, tzinfo=UTC)
    g = m.group_by("__c").agg(
        pl.len().cast(pl.Int32).alias("n_articles"),
        pl.col("domain_id").n_unique().clip(1, 32767).cast(pl.Int16).alias("n_sources"),
        pl.col("minute").min().alias("__m0"), pl.col("minute").max().alias("__m1"),
        pl.col("tone_x100").cast(pl.Float64).mean().round().cast(pl.Int16).alias("tone_x100"),
        pl.col("theme_mask").fill_null(0).bitwise_or().cast(pl.Int64).alias("theme_mask"),
        pl.col("sector_mask").fill_null(0).cast(pl.Int32).bitwise_or().cast(pl.Int16).alias("sector_mask"),
        pl.col("source_id").cast(pl.Int32).unique().alias("__src"),
        pl.col("n_events").fill_null(0).cast(pl.Int64).sum().clip(0, 32767).cast(pl.Int16).alias("n_events"),
        pl.col("url_hash").min().alias("__minhash"),
        pl.col("countries").list.contains(tx.GLOBAL_ID).sum().alias("__nglob"),
    )
    dom = (m.filter(pl.col("domain_id").is_not_null()).group_by("__c", "domain_id").agg(pl.len().alias("__n"),
                                                                                       pl.col("minute").min())
           .sort(["__c", "__n", "minute", "domain_id"], descending=[False, True, False, False])
           .group_by("__c", maintain_order=True).agg(pl.col("domain_id").head(5).alias("domain_ids")))
    urls = (m.filter(pl.col("url").is_not_null()).unique(["__c", "domain_id"], keep="first", maintain_order=True)
            .group_by("__c", maintain_order=True).agg(pl.col("url").head(3).alias("urls")))
    lang = (m.filter(pl.col("lang_id").is_not_null()).group_by("__c", "lang_id").len()
            .sort(["__c", "len"], descending=[False, True]).unique("__c", keep="first").select("__c", "lang_id"))
    cty = (m.select("__c", "countries").explode("countries").filter(pl.col("countries") != tx.GLOBAL_ID)
           .group_by("__c", "countries").len().sort(["__c", "len", "countries"], descending=[False, True, False])
           .group_by("__c", maintain_order=True).agg(pl.col("countries").head(tx.MAX_COUNTRIES).alias("__cty")))
    iss = (m.select("__c", "issuer_ids", "issuer_conf").filter(pl.col("issuer_ids").list.len() > 0)
           .explode(["issuer_ids", "issuer_conf"]).group_by("__c", "issuer_ids")
           .agg(pl.len().alias("__k"), pl.col("issuer_conf").cast(pl.Int32).max().alias("__conf")))
    g = (g.join(canon, on="__c", how="left").join(dom, on="__c", how="left").join(urls, on="__c", how="left")
         .join(lang, on="__c", how="left").join(cty, on="__c", how="left"))
    # issuer relevance: max conf x sqrt(member share); 100 when the issuer is named in the canonical headline
    if iss.height:
        iss = iss.join(g.select("__c", "n_articles", "title"), on="__c", how="left").with_columns(
            (pl.col("__conf") * (pl.col("__k") / pl.col("n_articles")).sqrt()).clip(0, 100).alias("__rel"))
        named = []
        for c_, iid, title in iss.select("__c", "issuer_ids", "title").iter_rows():
            name, tick = names.get(iid, ("", ""))
            tn = " " + re.sub(r"[^a-z0-9 ]", " ", hash_title(title)) + " "
            named.append(bool((name and f" {name} " in tn) or (tick and len(tick) >= 3 and f" {tick} " in tn)))
        iss = iss.with_columns(pl.when(pl.Series(named, dtype=pl.Boolean)).then(100.0).otherwise(pl.col("__rel"))
                               .round().cast(pl.Int8).alias("__rel"))
        iss = (iss.sort(["__c", "__rel", "issuer_ids"], descending=[False, True, False]).group_by("__c", maintain_order=True)
               .agg(pl.col("issuer_ids").head(10).cast(pl.Int32).alias("issuer_ids"),
                    pl.col("__rel").head(10).alias("issuer_rel")))
        g = g.join(iss, on="__c", how="left")
    else:
        g = g.with_columns(pl.lit(None, pl.List(pl.Int32)).alias("issuer_ids"), pl.lit(None, pl.List(pl.Int8)).alias("issuer_rel"))
    titles = g.get_column("title").to_list()
    if fixed_col is not None:
        sids = [int(x) for x in g.get_column("__c").to_list()]
    else:
        linked = link_neighbours(titles, neighbours, min_neighbour_articles)
        sids = [lk if lk is not None else hash64(f"story:{d.isoformat()}:{int(mh)}")
                for lk, mh in zip(linked, g.get_column("__minhash").to_list(), strict=True)]
    srcmask = [sum(1 << int(x) for x in set(xs or []) if x is not None and 0 <= x < 15) for xs in g.get_column("__src").to_list()]
    glob = (pl.col("__nglob") >= 0.3 * pl.col("n_articles")) | pl.col("__cty").is_null() | (pl.col("__cty").list.len() == 0)
    sdf = g.with_columns(
        pl.lit(d).alias("date"), pl.Series("story_id", sids, pl.Int64),
        pl.Series("title_hash", simhash_many([hash_title(t) for t in titles]), pl.Int64),
        (pl.lit(t0) + pl.duration(minutes=pl.col("__m0"))).alias("first_ts"),
        (pl.lit(t0) + pl.duration(minutes=pl.col("__m1"))).alias("last_ts"),
        pl.Series("source_mask", srcmask, pl.Int16),
        pl.col("domain_ids").fill_null(pl.lit([], pl.List(pl.Int32))),
        pl.col("urls").fill_null(pl.lit([], pl.List(pl.Utf8))),
        pl.when(glob).then(pl.concat_list([pl.col("__cty").fill_null(pl.lit([], pl.List(pl.Int16))),
                                           pl.lit([tx.GLOBAL_ID], pl.List(pl.Int16))]))
        .otherwise(pl.col("__cty")).alias("countries"),
        pl.col("issuer_ids").fill_null(pl.lit([], pl.List(pl.Int32))),
        pl.col("issuer_rel").fill_null(pl.lit([], pl.List(pl.Int8))),
        pl.col("title").str.slice(0, 300),
        pl.lit(None, pl.Int8).alias("sentiment_x100"), pl.lit(None, pl.Utf8).alias("sentiment_model"),
    ).with_columns(salience_expr().alias("salience"))
    smem = (m.join(sdf.select("__c", "story_id"), on="__c", how="left")
            .select("story_id", "url_hash", "source_id", "domain_id", "minute", "tone_x100", "lang_id")
            .with_columns(pl.lit(d).alias("date")).unique("url_hash", keep="first"))
    sdf = sdf.select([pl.col(c).cast(t) for c, t in STORY_SCHEMA.items()])
    # two day-clusters can map onto the same neighbour story: merge them into one story-day row
    if sdf.get_column("story_id").n_unique() < sdf.height:
        sdf = merge_story_rows(sdf)
    return sdf, smem


def merge_story_rows(sdf: pl.DataFrame) -> pl.DataFrame:
    """Combine rows sharing (story_id, date): counts add up, masks OR, labels union, canonical = biggest row."""
    out = []
    for _, g in sdf.sort("n_articles", descending=True).group_by(["story_id", "date"], maintain_order=True):
        if g.height == 1:
            out.append(g.row(0, named=True))
            continue
        r = dict(g.row(0, named=True))
        r["n_articles"] = int(g.get_column("n_articles").sum())
        r["n_sources"] = min(32767, int(g.get_column("n_sources").sum()))
        r["n_events"] = min(32767, int(g.get_column("n_events").sum()))
        r["first_ts"], r["last_ts"] = g.get_column("first_ts").min(), g.get_column("last_ts").max()
        for k in ("source_mask", "theme_mask", "sector_mask"):
            v = 0
            for x in g.get_column(k).to_list():
                v |= int(x or 0)
            r[k] = v
        doms: Counter = Counter()
        for lst in g.get_column("domain_ids").to_list():
            doms.update(lst or [])
        r["domain_ids"] = [k for k, _ in doms.most_common(5)]
        urls: list[str] = []
        for lst in g.get_column("urls").to_list():
            urls += [u for u in lst or [] if u not in urls]
        r["urls"] = urls[:3]
        rel: dict[int, int] = {}
        for ids, rs in zip(g.get_column("issuer_ids").to_list(), g.get_column("issuer_rel").to_list(), strict=True):
            for i, x in zip(ids or [], rs or [], strict=False):
                rel[i] = max(rel.get(i, 0), int(x or 0))
        top = sorted(rel.items(), key=lambda kv: -kv[1])[:10]
        r["issuer_ids"], r["issuer_rel"] = [i for i, _ in top], [x for _, x in top]
        cs: list[int] = []
        for lst in g.get_column("countries").to_list():
            cs += [c for c in lst or [] if c not in cs]
        r["countries"] = cs[:4]
        tones = [x for x in g.get_column("tone_x100").to_list() if x is not None]
        r["tone_x100"] = int(round(float(np.mean(tones)))) if tones else None
        r["salience"] = salience(r["n_articles"], r["n_sources"], r["n_events"], r["tone_x100"],
                                 max(r["issuer_rel"]) if r["issuer_rel"] else 0)
        out.append(r)
    return pl.DataFrame(out, schema=sdf.schema)


def compression_ratio(members: int, stories: int) -> float | None:
    return round(members / stories, 3) if stories else None
