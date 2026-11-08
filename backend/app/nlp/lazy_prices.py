"""Lazy Prices (Cohen, Malloy & Nguyen 2020): year-over-year text similarity of periodic filings (job `lazy_prices`,
owner signals-alt; process executor).

Input: SEC section texts extracted by ingest-us (data/raw/sec_text/<cik>/<accession>/<section>.txt.gz +
_index.parquet). Pairs: same company, same base form, same section; 10-K/20-F/40-F vs the previous annual report
(period_end 300-430 days earlier), 10-Q vs the 10-Q of the same fiscal quarter one year earlier (330-400 days).
Amendments are skipped. Sections: full, risk_factors (10-K Item 1A / 10-Q Part II 1A), mdna, legal, business -> other.

Similarity measures per pair:
  cosine_tfidf     cosine of sublinear-TF x IDF vectors (English stop words removed); IDF per section from a random
                   sample of <= 250 documents of the corpus (a corpus-level weighting constant: it never uses labels or
                   prices; computed streaming in worker processes -- the full-corpus hashing matrix ran out of memory)
  jaccard          |W1 & W2| / |W1 | W2| on word sets
  min_edit_ratio   mean over current sentences of the best rapidfuzz ratio vs previous sentences (exact repeats = 1)
  words_added_pct  share of current sentences with best ratio < 0.8 (new / substantially rewritten)
  LM deltas        change in Loughran-McDonald negative / uncertainty word share; MD&A tone = (pos - neg) / words
Cross-sectional PIT stats (vs filings of the same section & base form with knowledge_date in the prior 365 days):
  change_z (z of cosine; negative = unusually large change), universe_pct (percentile of cosine; low = big change),
  large_change (risk_factors or full with change_z <= -2 or words_added_pct >= 0.35).
knowledge_date = filing date (PIT).

params: full (recompute all pairs; default only filings not yet in filing_text_changes), workers (8), sections.
"""

from __future__ import annotations

import gzip
import math
import re
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table
from app.nlp.common import has_data

log = get_logger(__name__)

SECTION_MAP = {"full": "full", "risk_factors": "risk_factors", "mdna": "mdna", "legal": "legal", "business": "other"}
ANNUAL = {"10-K", "10-KT", "20-F", "40-F"}
QUARTERLY = {"10-Q", "10-QT"}
MAX_SENTENCES = 4000
EDIT_SAMPLE = 600

_WORD = re.compile(r"[a-z][a-z\-']+")
_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def base_form(form: str) -> str:
    return (form or "").upper().replace("/A", "").strip()


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def sentences(text: str) -> list[str]:
    out = []
    for s in _SENT_SPLIT.split(text):
        s = re.sub(r"\s+", " ", s).strip().lower()
        if len(s.split()) >= 5 and not re.fullmatch(r"[\d\s.,$%()\-]+", s):
            out.append(s)
    return out


def jaccard(a: set[str], b: set[str]) -> float | None:
    if not a and not b:
        return None
    return len(a & b) / max(len(a | b), 1)


def sentence_edit(cur: list[str], prev: list[str], seed: int = 0) -> tuple[float | None, float | None]:
    """(mean best similarity of current sentences vs previous, share of new sentences)."""
    if not cur or not prev:
        return None, None
    from rapidfuzz import fuzz, process  # noqa: PLC0415

    prev_set = set(prev)
    exact = np.array([s in prev_set for s in cur])
    rest = [s for s, e in zip(cur, exact, strict=True) if not e]
    n = len(cur)
    if not rest:
        return 1.0, 0.0
    rng = np.random.default_rng(seed)
    sample = rest if len(rest) <= EDIT_SAMPLE else [rest[i] for i in rng.choice(len(rest), EDIT_SAMPLE, replace=False)]
    pv = prev if len(prev) <= MAX_SENTENCES else [prev[i] for i in rng.choice(len(prev), MAX_SENTENCES, replace=False)]
    m = process.cdist(sample, pv, scorer=fuzz.ratio, workers=1, dtype=np.uint8)
    best = m.max(axis=1) / 100.0
    share_rest = len(rest) / n
    mean_best = (exact.sum() + best.mean() * len(rest)) / n
    new_share = float((best < 0.8).mean()) * share_rest
    return float(mean_best), new_share


def lm_shares(ws: list[str]) -> dict[str, float | None]:
    from app.nlp.lm_words import word_lists  # noqa: PLC0415

    wl = word_lists()
    n = len(ws)
    if n == 0:
        return {"neg": None, "pos": None, "unc": None}
    return {"neg": sum(w in wl["negative"] for w in ws) / n, "pos": sum(w in wl["positive"] for w in ws) / n,
            "unc": sum(w in wl["uncertainty"] for w in ws) / n}


def _read(path: str, data_dir: str) -> str:
    from pathlib import Path  # noqa: PLC0415

    p = Path(data_dir) / path
    try:
        return gzip.decompress(p.read_bytes()).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _stop_words() -> frozenset[str]:
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS  # noqa: PLC0415

    return frozenset(ENGLISH_STOP_WORDS)


def tfidf_vector(ws: list[str], idf: dict[str, float] | None, stop: frozenset[str]) -> dict[str, float]:
    """Sublinear TF x IDF (unseen terms get the max IDF)."""
    c = Counter(w for w in ws if w not in stop)
    if not idf:
        return {w: 1 + math.log(n) for w, n in c.items()}
    dflt = idf.get("__max__", 1.0)
    return {w: (1 + math.log(n)) * idf.get(w, dflt) for w, n in c.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float | None:
    if not a or not b:
        return None
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na > 0 and nb > 0 else None


def pair_metrics(cur_text: str, prev_text: str, seed: int = 0, idf: dict[str, float] | None = None) -> dict[str, Any]:
    """All measures for one pair of texts (cosine_tfidf uses `idf`; plain sublinear TF cosine when None)."""
    cw, pw = words(cur_text), words(prev_text)
    stop = _stop_words()
    cos = cosine(tfidf_vector(cw, idf, stop), tfidf_vector(pw, idf, stop))
    cs, ps = sentences(cur_text)[:MAX_SENTENCES], sentences(prev_text)[:MAX_SENTENCES]
    me, added = sentence_edit(cs, ps, seed)
    lc, lp = lm_shares(cw), lm_shares(pw)
    return {
        "cosine_tfidf": cos, "jaccard": jaccard(set(cw), set(pw)), "min_edit_ratio": me, "words_added_pct": added,
        "neg_word_delta": None if lc["neg"] is None or lp["neg"] is None else lc["neg"] - lp["neg"],
        "uncertainty_word_delta": None if lc["unc"] is None or lp["unc"] is None else lc["unc"] - lp["unc"],
        "tone_net": None if lc["neg"] is None else lc["pos"] - lc["neg"], "n_words": len(cw), "n_words_prev": len(pw),
    }


_IDF: dict[str, dict[str, float]] = {}


def _init_worker(idf: dict[str, dict[str, float]]) -> None:
    global _IDF  # noqa: PLW0603 - per-process read-only IDF tables
    _IDF = idf


def _pair_worker(args: tuple[str, str, str, int, str]) -> dict[str, Any]:
    cur_path, prev_path, data_dir, seed, section = args
    try:
        return pair_metrics(_read(cur_path, data_dir), _read(prev_path, data_dir), seed, _IDF.get(section))
    except MemoryError:  # host under memory pressure: skip the pair (re-tried on the next incremental run)
        return {"error": "MemoryError"}


def _df_worker(args: tuple[str, str]) -> list[str]:
    path, data_dir = args
    stop = _stop_words()
    return list({w for w in words(_read(path, data_dir)) if w not in stop})


def build_idf(pairs: pl.DataFrame, data_dir: str, workers: int = 8, sample: int = 250,
              seed: int = 0) -> dict[str, dict[str, float]]:
    """Per-section smoothed IDF from a random sample of <= `sample` documents (streamed in worker processes)."""
    rng = np.random.default_rng(seed)
    out: dict[str, dict[str, float]] = {}
    for sec in pairs.get_column("section").unique().to_list():
        paths = pairs.filter(pl.col("section") == sec).get_column("path").unique().sort().to_list()
        if len(paths) > sample:
            paths = [paths[i] for i in rng.choice(len(paths), sample, replace=False)]
        df: Counter[str] = Counter()
        args = [(p, data_dir) for p in paths]
        if workers > 1 and len(args) > 20:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for terms in ex.map(_df_worker, args, chunksize=4):
                    df.update(terms)
        else:
            for a in args:
                df.update(_df_worker(a))
        n = len(paths)
        idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items() if c >= 2}
        idf["__max__"] = math.log((1 + n) / 2) + 1
        out[sec] = idf
    return out


# ================================================================================================ pairing


def load_index(settings: Settings | None = None) -> pl.DataFrame:
    s = settings or get_settings()
    p = s.raw_dir / "sec_text" / "_index.parquet"
    if not p.exists():
        return pl.DataFrame()
    return pl.read_parquet(p)


def make_pairs(idx: pl.DataFrame, sections: list[str] | None = None) -> pl.DataFrame:
    """Current filing x section -> previous comparable filing (same cik, base form, section)."""
    if idx.is_empty():
        return idx
    d = idx.with_columns(
        pl.col("form_type").str.to_uppercase().str.replace("/A", "", literal=True).str.strip_chars().alias("base_form"),
        pl.col("form_type").str.to_uppercase().str.ends_with("/A").alias("is_amend"),
        pl.coalesce("period_end", "knowledge_date").alias("pe"),
        pl.col("section").replace_strict(SECTION_MAP, default=None).alias("sec_out"),
    ).filter(~pl.col("is_amend") & pl.col("sec_out").is_not_null()
             & pl.col("base_form").is_in(list(ANNUAL | QUARTERLY)) & (pl.col("n_words") > 100))
    if sections:
        d = d.filter(pl.col("sec_out").is_in(sections))
    d = d.with_columns(pl.when(pl.col("base_form").is_in(list(ANNUAL))).then(pl.lit("A")).otherwise(pl.lit("Q"))
                       .alias("kind"))
    left = d.select("filing_id", "cik", "base_form", "kind", "section", "sec_out", "pe", "knowledge_date", "path",
                    "form_type")
    right = d.select(pl.col("filing_id").alias("prev_filing_id"), "cik", "base_form", "section",
                     pl.col("pe").alias("prev_pe"), pl.col("knowledge_date").alias("prev_knowledge_date"),
                     pl.col("path").alias("prev_path"))
    j = left.join(right, on=["cik", "base_form", "section"], how="inner").with_columns(
        (pl.col("pe") - pl.col("prev_pe")).dt.total_days().alias("gap"))
    j = j.filter(pl.col("prev_knowledge_date") < pl.col("knowledge_date")).filter(
        pl.when(pl.col("kind") == "A").then(pl.col("gap").is_between(300, 430))
        .otherwise(pl.col("gap").is_between(330, 400)))
    j = j.with_columns((pl.col("gap") - 365).abs().alias("__dist")).sort("__dist").unique(
        ["filing_id", "section"], keep="first")
    return j.drop("__dist")


def pit_cross_section(df: pl.DataFrame, window_days: int = 365, min_n: int = 20) -> pl.DataFrame:
    """change_z / universe_pct of cosine vs same (section, base_form) filings known in the prior `window_days`."""
    out_z, out_p = [], []
    df = df.with_row_index("__r")
    res = {}
    for (_sec, _bf), g in df.group_by(["section", "base_form"]):
        g = g.sort("knowledge_date")
        kd = g.get_column("knowledge_date").to_numpy()
        cs = g.get_column("cosine_tfidf").to_numpy().astype(float)
        rows = g.get_column("__r").to_list()
        for i in range(len(g)):
            lo = kd[i] - np.timedelta64(window_days, "D")
            m = (kd >= lo) & (kd < kd[i]) & np.isfinite(cs)
            ref = cs[m]
            if len(ref) >= min_n and np.isfinite(cs[i]):
                sd = ref.std()
                res[rows[i]] = ((cs[i] - ref.mean()) / sd if sd > 1e-9 else 0.0, float((ref <= cs[i]).mean()))
            else:
                res[rows[i]] = (None, None)
    for r in df.get_column("__r").to_list():
        z, p = res.get(r, (None, None))
        out_z.append(z)
        out_p.append(p)
    return df.with_columns(pl.Series("change_z", out_z, pl.Float64), pl.Series("universe_pct", out_p, pl.Float64)).drop(
        "__r")


# ================================================================================================ job


def compute(settings: Settings | None = None, full: bool = False, workers: int = 8,
            sections: list[str] | None = None, ctx: Any | None = None) -> tuple[pl.DataFrame, dict[str, Any]]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    idx = load_index(s)
    pairs = make_pairs(idx, sections)
    stats: dict[str, Any] = {"index_rows": idx.height, "pairs": pairs.height}
    if pairs.is_empty():
        return pl.DataFrame(), stats
    existing = None
    if has_data("filing_text_changes", s):
        existing = scan_table("filing_text_changes", s).collect()
    if not full and existing is not None and existing.height:
        done = existing.select(pl.concat_str(["filing_id", "section"], separator="|")).to_series()
        pairs = pairs.filter(~pl.concat_str(["filing_id", pl.col("sec_out")], separator="|").is_in(done.implode()))
    stats["pairs_new"] = pairs.height
    if pairs.is_empty():
        return pl.DataFrame(), stats
    data_dir = str(s.data_dir)
    idf = build_idf(pairs, data_dir, workers)
    if ctx:
        ctx.check_cancelled()
    args = [(r["path"], r["prev_path"], data_dir, i, r["section"]) for i, r in enumerate(
        pairs.select("path", "prev_path", "section").iter_rows(named=True))]
    if workers > 1 and len(args) > 20:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(idf,)) as ex:
            mets = list(ex.map(_pair_worker, args, chunksize=4))
    else:
        _init_worker(idf)
        mets = [_pair_worker(a) for a in args]
    m = pl.DataFrame(mets, infer_schema_length=None)
    if "error" in m.columns:
        stats["pair_errors"] = int(m.get_column("error").is_not_null().sum())
        keep = m.get_column("error").is_null()
        pairs, m = pairs.filter(keep), m.filter(keep).drop("error")
    out = pl.concat([pairs, m], how="horizontal")
    # issuer ids
    from app.nlp.common import resolve_entities  # noqa: PLC0415

    ids = resolve_entities(out.select(pl.col("cik").alias("source_entity_id")).unique().with_columns(
        pl.lit("sec_edgar").alias("source_id")))
    out = out.join(ids.select(pl.col("source_entity_id").alias("cik"), "issuer_id"), on="cik", how="left")
    stats["unresolved_cik_rows"] = out.filter(pl.col("issuer_id").is_null()).height
    allrows = out
    if existing is not None and existing.height and "base_form" in existing.columns:
        prev = existing.select("filing_id", pl.col("section").alias("sec_out"), "base_form", "knowledge_date",
                               "cosine_tfidf").filter(~pl.struct("filing_id", "sec_out").is_in(
            out.select(pl.struct("filing_id", "sec_out")).to_series().implode()))
        allrows = pl.concat([prev, out.select("filing_id", "sec_out", "base_form", "knowledge_date", "cosine_tfidf")],
                            how="diagonal_relaxed")
    cx = pit_cross_section(allrows.select("filing_id", pl.col("sec_out").alias("section"), "base_form",
                                          "knowledge_date", "cosine_tfidf"))
    out = out.join(cx.select("filing_id", pl.col("section").alias("sec_out"), "change_z", "universe_pct"),
                   on=["filing_id", "sec_out"], how="left")
    out = out.with_columns(
        (pl.col("sec_out").is_in(["risk_factors", "full"])
         & ((pl.col("change_z") <= -2) | (pl.col("words_added_pct") >= 0.35))).fill_null(False).alias("large_change"))
    res = out.select(
        "filing_id", "prev_filing_id", "issuer_id", pl.col("knowledge_date").dt.year().cast(pl.Int16).alias("year"),
        pl.col("sec_out").alias("section"), "cosine_tfidf", "jaccard", "min_edit_ratio", "words_added_pct",
        "neg_word_delta", "uncertainty_word_delta", pl.lit("en").alias("language"), "knowledge_date",
        pl.lit(datetime.now(UTC)).alias("computed_at"), "form_type", "base_form",
        pl.col("cik").alias("source_entity_id"), "prev_knowledge_date", "tone_net", "change_z", "universe_pct",
        "large_change", "n_words", "n_words_prev")
    stats["seconds"] = round(time.perf_counter() - t0, 1)
    stats["pairs_per_s"] = round(res.height / max(stats["seconds"], 1e-9), 2)
    return res, stats


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    res, st = compute(ctx.settings, bool(p.get("full")), int(p.get("workers", 8)), p.get("sections"), ctx)
    rows = {}
    if res.height:
        ok = res.filter(pl.col("issuer_id").is_not_null())
        rows["filing_text_changes"] = write_table(ok, "filing_text_changes", settings=ctx.settings)
        st["large_changes"] = int(ok.get_column("large_change").sum())
    return JobResult(status="success", rows_written=rows, entities_ok=rows.get("filing_text_changes", 0),
                     message=str(st))

