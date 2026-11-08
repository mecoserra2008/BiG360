"""News sentiment scoring (job `nlp_sentiment`, owner signals-alt; process executor).

* Language: news_items.language when present (GDELT srclc / provider), else `detect_language()` (script ranges for
  CJK / Hangul / Kana / Cyrillic / Arabic / Greek / Thai / Hebrew / Devanagari, then a stop-word vote among Latin-script
  languages; default "en").
* Models (CPU, batched, torch threads = params.threads, default 8):
    en     -> ProsusAI/finbert                              (labels positive / negative / neutral)
    other  -> cardiffnlp/twitter-xlm-roberta-base-sentiment (labels negative / neutral / positive)
  Score = P(pos) - P(neg) in [-1, 1]; label = argmax.
* Persistent cache: data/cache/nlp/sentiment/<model>.parquet keyed by sha1(text) -> probabilities, so re-runs and
  syndicated duplicates cost nothing.
* Relevance per (item, linked issuer) in [0, 1]: ticker / company name in the title 1.0; provider ticker tag (Polygon
  tickers[]) 0.85; name in the snippet 0.7; organisation mention only (GKG) 0.5 x org-link confidence
  (app.nlp.gdelt_link.OrgLinker, ambiguous names such as Apple / Meta / Visa need corroborating context); items linked
  to many issuers are down-weighted (x sqrt(3/n) when n > 3).
* Written back to news_items (two-writer exception: only sentiment_* / relevance columns change; the job runs strictly
  after ingestion): sentiment_label, sentiment_score, sentiment_model, relevance (max), issuer_relevance (per issuer_ids).

params: threads (8), batch_size (64), max_length (96), rescore (false: only unscored items), since (YYYY-MM-DD),
        limit (max items), relevance_only (recompute relevance without scoring), chunk (5000, written per chunk),
        max_minutes (100: stop after the current chunk -> status partial; the next run continues).
"""

from __future__ import annotations

import hashlib
import html
import re
import time
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np
import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table

log = get_logger(__name__)

EN_MODEL = "ProsusAI/finbert"
ML_MODEL = "cardiffnlp/twitter-xlm-roberta-base-sentiment"
LABELS = ("negative", "neutral", "positive")

# ================================================================================================ language

_SCRIPTS = [
    ("ko", r"[가-힯ᄀ-ᇿ]"), ("ja", r"[぀-ヿ]"), ("zh", r"[一-鿿]"),
    ("ru", r"[Ѐ-ӿ]"), ("ar", r"[؀-ۿ]"), ("el", r"[Ͱ-Ͽ]"), ("th", r"[฀-๿]"),
    ("he", r"[֐-׿]"), ("hi", r"[ऀ-ॿ]"),
]
_STOP = {
    "en": "the of and to in for on with is are at from by as this that will after over new says",
    "de": "der die das und mit für nicht von den ist im zu auf bei nach über eine ein wird",
    "fr": "le la les des et pour avec dans sur est une un du au aux par qui sont pas",
    "es": "el la los las y para con en del por una un que se es más sobre tras",
    "pt": "o a os as e para com em do da dos das por uma um que se não mais após",
    "it": "il lo la gli le e per con del della nel che una un sono più dopo alla",
    "nl": "de het een en van in op met voor is niet aan bij naar door zijn",
}
_STOPSETS = {k: set(v.split()) for k, v in _STOP.items()}


def detect_language(text: str | None, hint: str | None = None) -> str:
    if hint and re.fullmatch(r"[a-z]{2}", hint):
        return hint
    if not text:
        return "en"
    for lang, pat in _SCRIPTS:
        if len(re.findall(pat, text)) >= max(2, 0.2 * len(text.replace(" ", ""))) or (
                lang in ("ko", "ja") and re.search(pat, text)):
            return lang
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    if not words:
        return "en"
    votes = {k: sum(w in s for w in words) for k, s in _STOPSETS.items()}
    best = max(votes, key=lambda k: (votes[k], k == "en"))
    return best if votes[best] > 0 else "en"


def clean_text(title: str | None, snippet: str | None = None, max_chars: int = 400) -> str:
    t = html.unescape(html.unescape(title or "")).strip()
    if snippet:
        sn = html.unescape(snippet).strip()
        if sn and sn[:60].lower() != t[:60].lower():
            t = f"{t}. {sn}"
    return re.sub(r"\s+", " ", t)[:max_chars]


def text_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ================================================================================================ models


class HFSentiment:
    """Lazy-loaded Hugging Face sequence classifier returning probabilities ordered as LABELS."""

    def __init__(self, model_id: str, threads: int = 8, max_length: int = 96):
        self.model_id = model_id
        self.threads = threads
        self.max_length = max_length
        self._tok = None
        self._model = None
        self._order: list[int] | None = None

    def load(self) -> None:
        if self._model is not None:
            return
        import torch  # noqa: PLC0415
        from transformers import AutoModelForSequenceClassification, AutoTokenizer  # noqa: PLC0415

        torch.set_num_threads(self.threads)
        self._tok = AutoTokenizer.from_pretrained(self.model_id)
        self._model = AutoModelForSequenceClassification.from_pretrained(self.model_id).eval()
        id2 = {int(k): str(v).lower() for k, v in self._model.config.id2label.items()}
        fallback = {"label_0": "negative", "label_1": "neutral", "label_2": "positive"}
        id2 = {k: fallback.get(v, v) for k, v in id2.items()}
        self._order = [next(i for i, v in id2.items() if v.startswith(lab[:3])) for lab in LABELS]

    def predict(self, texts: Sequence[str], batch_size: int = 64) -> np.ndarray:
        import torch  # noqa: PLC0415

        self.load()
        out = np.zeros((len(texts), 3), dtype=np.float32)
        # sort by length so batches are padded minimally
        order = np.argsort([len(t) for t in texts])
        with torch.inference_mode():
            for i in range(0, len(texts), batch_size):
                idx = order[i:i + batch_size]
                enc = self._tok([texts[j] for j in idx], padding=True, truncation=True, max_length=self.max_length,
                                return_tensors="pt")
                probs = torch.softmax(self._model(**enc).logits, dim=-1).numpy()
                out[idx] = probs[:, self._order]
        return out


class SentimentCache:
    def __init__(self, model_id: str, settings: Settings | None = None):
        s = settings or get_settings()
        self.path = s.cache_dir / "nlp" / "sentiment" / (re.sub(r"[^A-Za-z0-9]+", "_", model_id) + ".parquet")
        self._d: dict[str, tuple[float, float, float]] = {}
        self._new: dict[str, tuple[float, float, float]] = {}
        if self.path.exists():
            df = pl.read_parquet(self.path)
            self._d = {k: (a, b, c) for k, a, b, c in df.iter_rows()}

    def get(self, key: str) -> tuple[float, float, float] | None:
        return self._d.get(key) or self._new.get(key)

    def put(self, key: str, probs: Sequence[float]) -> None:
        self._new[key] = (float(probs[0]), float(probs[1]), float(probs[2]))

    def flush(self) -> None:
        if not self._new:
            return
        self._d.update(self._new)
        self._new = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keys = list(self._d)
        arr = np.array([self._d[k] for k in keys], dtype=np.float64)
        df = pl.DataFrame({"key": keys, "p_neg": arr[:, 0], "p_neu": arr[:, 1], "p_pos": arr[:, 2]})
        tmp = self.path.with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.replace(self.path)

    def __len__(self) -> int:
        return len(self._d) + len(self._new)


class SentimentScorer:
    """Routes texts to FinBERT (en) or XLM-R (other languages), with the on-disk cache."""

    def __init__(self, settings: Settings | None = None, threads: int = 8, batch_size: int = 64, max_length: int = 96,
                 models: dict[str, Any] | None = None):
        self.settings = settings or get_settings()
        self.batch_size = batch_size
        self.models = models or {"en": HFSentiment(EN_MODEL, threads, max_length),
                                 "xx": HFSentiment(ML_MODEL, threads, max_length)}
        self.caches = {k: SentimentCache(m.model_id, self.settings) for k, m in self.models.items()}
        self.stats = {"scored": 0, "cache_hits": 0, "seconds_model": 0.0}

    def score(self, texts: Sequence[str], langs: Sequence[str]) -> pl.DataFrame:
        n = len(texts)
        probs = np.full((n, 3), np.nan)
        model_ids: list[str | None] = [None] * n
        for route in ("en", "xx"):
            idx = [i for i in range(n) if (langs[i] == "en") == (route == "en") and texts[i]]
            if not idx:
                continue
            cache = self.caches[route]
            todo: dict[str, list[int]] = {}
            for i in idx:
                k = text_key(texts[i])
                hit = cache.get(k)
                model_ids[i] = self.models[route].model_id
                if hit is not None:
                    probs[i] = hit
                    self.stats["cache_hits"] += 1
                else:
                    todo.setdefault(k, []).append(i)
            if todo:
                keys = list(todo)
                t0 = time.perf_counter()
                p = self.models[route].predict([texts[todo[k][0]] for k in keys], self.batch_size)
                self.stats["seconds_model"] += time.perf_counter() - t0
                self.stats["scored"] += len(keys)
                for k, row in zip(keys, p, strict=True):
                    cache.put(k, row)
                    for i in todo[k]:
                        probs[i] = row
                cache.flush()
        lab = np.array(LABELS)[np.nan_to_num(probs, nan=-1).argmax(axis=1)]
        ok = ~np.isnan(probs).any(axis=1)
        return pl.DataFrame({
            "sentiment_label": [lab[i] if ok[i] else None for i in range(n)],
            "sentiment_score": [float(probs[i, 2] - probs[i, 0]) if ok[i] else None for i in range(n)],
            "sentiment_model": model_ids,
            "p_neg": probs[:, 0], "p_neu": probs[:, 1], "p_pos": probs[:, 2],
        }, schema={"sentiment_label": pl.Utf8, "sentiment_score": pl.Float64, "sentiment_model": pl.Utf8,
                   "p_neg": pl.Float64, "p_neu": pl.Float64, "p_pos": pl.Float64})


# ================================================================================================ relevance


def _name_pattern(name: str | None) -> str | None:
    from app.ingestion.gdelt_gkg import norm_org  # noqa: PLC0415

    if not name:
        return None
    n = norm_org(name)
    return n if len(n) >= 3 else None


def relevance_frame(items: pl.DataFrame, secmap: pl.DataFrame, linker: Any | None = None) -> pl.DataFrame:
    """[item_id, issuer_relevance (aligned with issuer_ids), relevance (max)] for items with linked issuers."""
    from app.ingestion.gdelt_gkg import norm_org  # noqa: PLC0415

    info = {r["issuer_id"]: r for r in secmap.iter_rows(named=True)}
    ids, rels, maxes = [], [], []
    for r in items.select("item_id", "title", "snippet", "issuer_ids", "tickers", "entities", "themes",
                          "source_id").iter_rows(named=True):
        iss = r["issuer_ids"] or []
        title = clean_text(r["title"])
        t_norm = " " + norm_org(title) + " " if title else " "
        tl = title.lower()
        snip = " " + norm_org(clean_text(r["snippet"])) + " " if r["snippet"] else " "
        tickers = {t.upper() for t in (r["tickers"] or [])}
        ents = r["entities"] or []
        out = []
        for iid in iss:
            meta = info.get(iid) or {}
            tick = (meta.get("ticker") or "").upper()
            names = [x for x in {_name_pattern(meta.get("short_name")), _name_pattern(meta.get("name"))} if x]
            rel = 0.0
            in_title = any(f" {n} " in t_norm for n in names) or (
                tick and len(tick) >= 2 and re.search(rf"(\$|\(|nyse:\s*|nasdaq:\s*){re.escape(tick.lower())}\b", tl))
            if in_title:
                rel = 1.0
            elif tick and tick in tickers:
                rel = 0.85
            elif any(f" {n} " in snip for n in names):
                rel = 0.7
            elif r["source_id"] in ("gdelt_gkg", "gdelt_doc"):
                conf = 1.0
                if linker is not None and ents:
                    confs = [linker.confidence(norm_org(e), title, r["themes"], iid)[1] for e in ents]
                    conf = max(confs) if confs else 0.6
                rel = 0.5 * conf
            else:
                rel = 0.4
            if title and any(f" {n} " in t_norm for n in names):
                rel = 1.0
            out.append(rel)
        if len(iss) > 3:
            out = [x * (3 / len(iss)) ** 0.5 for x in out]
        ids.append(r["item_id"])
        rels.append([round(x, 4) for x in out])
        maxes.append(max(out) if out else None)
    return pl.DataFrame({"item_id": ids, "issuer_relevance": rels, "relevance": maxes},
                        schema={"item_id": pl.Utf8, "issuer_relevance": pl.List(pl.Float64), "relevance": pl.Float64})


def provider_score(ps: str | None, tickers: Sequence[str] | None = None) -> float | None:
    """Polygon insights JSON -> mean of positive=+1 / neutral=0 / negative=-1 (optionally for given tickers)."""
    if not ps:
        return None
    try:
        rows = orjson.loads(ps)
    except orjson.JSONDecodeError:
        return None
    m = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}
    vals = [m[str(r.get("sentiment", "")).lower()] for r in rows if isinstance(r, dict)
            and str(r.get("sentiment", "")).lower() in m and (not tickers or r.get("ticker") in tickers)]
    return float(np.mean(vals)) if vals else None


# ================================================================================================ job


def _select_items(s: Settings, p: dict[str, Any]) -> pl.DataFrame:
    lf = scan_table("news_items", s)
    if p.get("since"):
        lf = lf.filter(pl.col("knowledge_date") >= date.fromisoformat(str(p["since"])))
    df = lf.collect()
    for c, dt in (("relevance", pl.Float64), ("issuer_relevance", pl.List(pl.Float64))):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dt).alias(c))
    if not p.get("rescore") and not p.get("relevance_only"):
        df = df.filter(pl.col("sentiment_model").is_null() | pl.col("relevance").is_null())
    if p.get("limit"):
        df = df.head(int(p["limit"]))
    return df


def score_items(df: pl.DataFrame, scorer: SentimentScorer, rescore: bool = False) -> pl.DataFrame:
    texts = [clean_text(t, sn) for t, sn in zip(df["title"].to_list(), df["snippet"].to_list(), strict=True)]
    langs = [detect_language(t, h) for t, h in zip(texts, df["language"].to_list(), strict=True)]
    todo = df.get_column("sentiment_model").is_null().to_list() if not rescore else [True] * df.height
    sel = [i for i in range(df.height) if todo[i]]
    res = scorer.score([texts[i] for i in sel], [langs[i] for i in sel]) if sel else None
    lab = df.get_column("sentiment_label").to_list()
    sc = df.get_column("sentiment_score").to_list()
    mo = df.get_column("sentiment_model").to_list()
    if res is not None:
        for j, i in enumerate(sel):
            lab[i], sc[i], mo[i] = res["sentiment_label"][j], res["sentiment_score"][j], res["sentiment_model"][j]
    lang_out = [h if h else lg for h, lg in zip(df["language"].to_list(), langs, strict=True)]
    return df.with_columns(pl.Series("sentiment_label", lab, pl.Utf8), pl.Series("sentiment_score", sc, pl.Float64),
                           pl.Series("sentiment_model", mo, pl.Utf8), pl.Series("language", lang_out, pl.Utf8))


def run(ctx):  # noqa: ANN001 - JobContext
    from app.nlp.common import security_map  # noqa: PLC0415
    from app.nlp.gdelt_link import OrgLinker  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    p = ctx.params or {}
    t0 = time.perf_counter()
    df = _select_items(s, p)
    if df.is_empty():
        return JobResult(status="success", message="nothing to score")
    scorer = SentimentScorer(s, int(p.get("threads", 8)), int(p.get("batch_size", 64)), int(p.get("max_length", 96)))
    secmap, linker = security_map(s), OrgLinker.from_reference(s)
    # priority: items relevant to an issuer (relevance >= 0.5 -> they drive sentiment_daily) first, newest first;
    # every chunk is written so a timeout/cancel keeps progress and the next run continues
    pre = relevance_frame(df, secmap, linker).select("item_id", pl.col("relevance").alias("__pr"))
    df = df.join(pre, on="item_id", how="left").sort(
        [pl.col("__pr").fill_null(0.0) >= 0.5, "published_at"], descending=[True, True]).drop("__pr")
    chunk = int(p.get("chunk", 5000))
    n = 0
    budget = float(p.get("max_minutes", 100)) * 60
    for i in range(0, df.height, chunk):
        ctx.check_cancelled()
        part = df.slice(i, chunk)
        if not p.get("relevance_only"):
            part = score_items(part, scorer, bool(p.get("rescore")))
        rel = relevance_frame(part, secmap, linker)
        part = part.drop([c for c in ("relevance", "issuer_relevance") if c in part.columns]).join(
            rel, on="item_id", how="left")
        # a few legacy rows carry null issuer_ids (contract: non-null list) -> empty list, nothing else changes
        part = part.with_columns(pl.col("issuer_ids").fill_null(pl.lit([], pl.List(pl.Utf8))))
        n += write_table(part, "news_items", settings=s)
        log.info("nlp_sentiment chunk %d: items=%d scored=%d", i // chunk, n, scorer.stats["scored"])
        if time.perf_counter() - t0 > budget:
            break
    secs = time.perf_counter() - t0
    st = scorer.stats
    rate = st["scored"] / st["seconds_model"] if st["seconds_model"] > 0 else None
    msg = (f"items={n}/{df.height} model_scored={st['scored']} cache_hits={st['cache_hits']} "
           f"model_rate={rate and round(rate, 1)}/s total={secs:.0f}s")
    log.info(msg)
    return JobResult(status="success" if n >= df.height else "partial", rows_written={"news_items": n},
                     entities_ok=n, message=msg)


__all__ = ["detect_language", "clean_text", "SentimentScorer", "HFSentiment", "SentimentCache", "relevance_frame",
           "provider_score", "score_items", "run"]
