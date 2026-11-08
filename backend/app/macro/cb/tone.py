"""W3.6 Communication tone (job cb_tone) - MODEL-BASED.

Documents (official sites only), one per past policy meeting since 2020:
  fed  FOMC statement   https://www.federalreserve.gov/newsevents/pressreleases/monetary{YYYYMMDD}a.htm
                        (+ dissent count parsed from the official "Voting against ..." sentence)
  ecb  Monetary policy decisions press release (URL from the ECB yearly press-release index, cb_meetings.url)
  boe  Monetary Policy Summary and minutes   https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/{YYYY}/{month}-{YYYY}
  boj  Statement on Monetary Policy (PDF)   links from https://www.boj.or.jp/en/mopo/mpmdeci/state_{YYYY}/index.htm
  boc  FAD press release https://www.bankofcanada.ca/{YYYY}/{MM}/fad-press-release-{YYYY}-{MM}-{DD}/

Scoring (method ``tone_v1``): sentences (40-600 chars, max 120 per document) are classified by a local CPU model,
``mrince/CBRT-RoBERTa-HawkishDovish-Classifier`` (MIT licence; RoBERTa-base fine-tuned on central-bank MPC texts;
labels 0 neutral, 1 hawkish, 2 dovish). tone_model = mean(P(hawkish) - P(dovish)) in [-1, 1];
hawkish_share / dovish_share = share of sentences whose argmax is hawkish / dovish. A transparent lexicon score
(tone_lexicon, phrase lists below; per sentence (h - d) / (h + d), averaged over sentences with a match) is stored
alongside and used when the model cannot be loaded. The gated, CC-BY-NC FOMC-RoBERTa (gtfintechlab) was not used.
Scores are model-based text measures, not policy signals.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

from .common import http_get, now_utc, read_table_safe, update_state, write

log = get_logger(__name__)
METHOD = "tone_v1"
MODEL_ID = "mrince/CBRT-RoBERTa-HawkishDovish-Classifier"
SINCE = date(2020, 1, 1)
MAX_SENT = 120
BANKS = ("fed", "ecb", "boe", "boj", "boc")
DOC_TTL = 120 * 86400  # published statements are immutable

HAWK = ("raise the target", "raising the target", "increase the target", "increase interest rates", "raise interest rates",
        "rate increase", "rate hike", "hike", "tighten", "tightening", "firming", "restrictive", "further increases",
        "upside risks to inflation", "upside risk to inflation", "inflation remains elevated", "elevated inflation",
        "persistent inflation", "inflationary pressures", "price pressures", "overheating", "tight labour market",
        "tight labor market", "strong demand", "wage growth remains strong", "reduce its holdings", "balance sheet reduction",
        "vigilant", "not yet sufficiently", "higher for longer", "upward pressure")
DOVE = ("lower the target", "lowering the target", "reduce the target", "cut", "reduction in", "lower interest rates",
        "rate reduction", "easing", "ease", "accommodative", "accommodation", "downside risks", "downside risk",
        "weaker", "weakening", "slowdown", "slowed", "softening", "unemployment has risen", "unemployment rose",
        "disinflation", "inflation has eased", "inflation has declined", "inflation has fallen", "support the economy",
        "support economic activity", "further reductions", "spare capacity", "subdued", "below target")


# =============================================================================================== text

def extract_paragraphs(html: bytes | str) -> list[str]:
    from bs4 import BeautifulSoup  # noqa: PLC0415

    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        t.decompose()
    for sel in ("#article", "main article", "article", "main", "#main-content", ".page-content", "body"):
        node = soup.select_one(sel)
        if node is None:
            continue
        ps = [re.sub(r"\s+", " ", p.get_text(" ", strip=True)) for p in node.find_all(["p", "li"])]
        ps = [p for p in ps if len(p) >= 40]
        if len(ps) >= 3:
            return ps
    return []


def pdf_text(content: bytes) -> list[str]:
    from pypdf import PdfReader  # noqa: PLC0415

    r = PdfReader(io.BytesIO(content))
    txt = "\n".join((p.extract_text() or "") for p in r.pages)
    txt = re.sub(r"-\n(?=[a-z])", "", txt)
    paras = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"\n\s*\n|\n(?=\d+\.\s)", txt)]
    return [p for p in paras if len(p) >= 40]


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def sentences(paras: Sequence[str], max_n: int = MAX_SENT) -> list[str]:
    out = []
    for p in paras:
        for s in _SENT.split(p):
            s = s.strip()
            if 40 <= len(s) <= 600 and not s.lower().startswith(("voting for", "voting against", "for media inquiries",
                                                                   "implementation note", "share", "related")):
                out.append(s)
    return out[:max_n]


_VOTE = re.compile(r"(?:by a|majority of|by an?)\s+(?:majority\s+of\s+)?(\d{1,2})\s*[–—-]\s*(\d{1,2})"
                   r"(?:\s+(?:majority\s+)?vote)?", re.I)


def vote_split(paras: Sequence[str]) -> tuple[int | None, str | None]:
    """Dissents and vote text from the official document ('by a 12-0 vote', 'by a 7-2 majority vote', 'voted by a
    majority of 6-3'); FOMC fallback: names listed after 'Voting against the action were ...'."""
    txt = " ".join(paras)
    m = _VOTE.search(txt)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return b, f"{a}-{b}"
    m = re.search(r"Voting against (?:the|this) action (?:were|was):?\s+(.+?)(?:,?\s+who\b|;|(?<=[a-z]{2})\.\s+(?=[A-Z])|\.\s*$)",
                  txt)
    if m:
        names = [x for x in re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group(1)) if x.strip()]
        return len(names), f"{len(names)} against"
    return None, None


def fed_dissents(paras: Sequence[str]) -> tuple[int, str | None]:
    n, txt = vote_split(paras)
    return (n or 0), txt


def lexicon_score(sents: Sequence[str]) -> float | None:
    vals = []
    for s in sents:
        low = s.lower()
        h = sum(len(re.findall(r"\b" + re.escape(w) + r"\b", low)) for w in HAWK)
        d = sum(len(re.findall(r"\b" + re.escape(w) + r"\b", low)) for w in DOVE)
        if h + d:
            vals.append((h - d) / (h + d))
    return float(np.mean(vals)) if vals else None


# =============================================================================================== model

class ToneModel:
    def __init__(self, model_id: str = MODEL_ID, threads: int = 8, max_length: int = 160):
        self.model_id, self.threads, self.max_length = model_id, threads, max_length
        self._tok = self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        import torch  # noqa: PLC0415
        from transformers import AutoModelForSequenceClassification, AutoTokenizer  # noqa: PLC0415

        torch.set_num_threads(self.threads)
        self._tok = AutoTokenizer.from_pretrained(self.model_id)
        self._model = AutoModelForSequenceClassification.from_pretrained(self.model_id).eval()

    def predict(self, texts: Sequence[str], batch_size: int = 32) -> np.ndarray:
        """Probabilities [neutral, hawkish, dovish] per text."""
        import torch  # noqa: PLC0415

        self.load()
        out = []
        with torch.inference_mode():
            for i in range(0, len(texts), batch_size):
                enc = self._tok(list(texts[i:i + batch_size]), padding=True, truncation=True,
                                max_length=self.max_length, return_tensors="pt")
                out.append(torch.softmax(self._model(**enc).logits, dim=-1).numpy())
        return np.vstack(out) if out else np.zeros((0, 3))


class ScoreCache:
    def __init__(self, s: Settings, model_id: str = MODEL_ID):
        self.path = s.cache_dir / "nlp" / "cb_tone" / (re.sub(r"[^A-Za-z0-9]+", "_", model_id) + ".parquet")
        self.data: dict[str, tuple[float, float, float]] = {}
        if self.path.exists():
            try:
                for k, a, b, c in pl.read_parquet(self.path).iter_rows():
                    self.data[k] = (a, b, c)
            except Exception:  # noqa: BLE001
                self.data = {}
        self.dirty = False

    @staticmethod
    def key(t: str) -> str:
        return hashlib.sha1(t.encode("utf-8")).hexdigest()[:20]

    def flush(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame([(k, *v) for k, v in self.data.items()], schema=["key", "p_neutral", "p_hawk", "p_dove"],
                     orient="row").write_parquet(self.path)
        self.dirty = False


def score_doc(sents: Sequence[str], model: ToneModel | None, cache: ScoreCache | None) -> dict[str, Any]:
    res: dict[str, Any] = {"n_sentences": len(sents), "tone_lexicon": lexicon_score(sents), "tone_model": None,
                           "hawkish_share": None, "dovish_share": None}
    if not sents or model is None:
        return res
    keys = [ScoreCache.key(s) for s in sents]
    todo = [i for i, k in enumerate(keys) if cache is None or k not in cache.data]
    if todo:
        probs = model.predict([sents[i] for i in todo])
        for i, p in zip(todo, probs, strict=False):
            if cache is not None:
                cache.data[keys[i]] = (float(p[0]), float(p[1]), float(p[2]))
                cache.dirty = True
    arr = np.array([cache.data[k] if cache is not None else (0, 0, 0) for k in keys]) if cache else model.predict(sents)
    res["tone_model"] = float(np.mean(arr[:, 1] - arr[:, 2]))
    am = arr.argmax(axis=1)
    res["hawkish_share"] = float(np.mean(am == 1))
    res["dovish_share"] = float(np.mean(am == 2))
    return res


# =============================================================================================== documents

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")


def doc_url(bank: str, d: date, meeting_url: str | None) -> str | None:
    if bank == "fed":
        return f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{d:%Y%m%d}a.htm"
    if bank == "ecb":
        return meeting_url if meeting_url and "/press/pr/" in meeting_url else None
    if bank == "boe":
        return f"https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/{d.year}/{MONTHS[d.month - 1]}-{d.year}"
    if bank == "boc":
        return f"https://www.bankofcanada.ca/{d:%Y}/{d:%m}/fad-press-release-{d:%Y-%m-%d}/"
    return None


def boj_links(year: int, s: Settings) -> dict[date, str]:
    raw = http_get("boj_web", f"https://www.boj.or.jp/en/mopo/mpmdeci/state_{year}/index.htm", ttl=7 * 86400
                   if year < now_utc().year else 12 * 3600, settings=s).decode("utf-8", errors="replace")
    out = {}
    for m in re.finditer(r'href="(/en/mopo/mpmdeci/[a-z_]+\d{4}/k(\d{2})(\d{2})(\d{2})a\.(?:pdf|htm))"', raw):
        try:
            d = date(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            continue
        out.setdefault(d, "https://www.boj.or.jp" + m.group(1))
    return out


def fetch_doc(bank: str, url: str, s: Settings) -> list[str]:
    content = http_get(f"{bank}_web", url, ttl=DOC_TTL, settings=s, browser=bank == "boe", timeout=120)
    if url.lower().endswith(".pdf"):
        return pdf_text(content)
    return extract_paragraphs(content)


def build_tone(s: Settings | None = None, use_model: bool = True, write_out: bool = True,
               since: date = SINCE) -> dict[str, Any]:
    s = s or get_settings()
    today = now_utc().date()
    meet = read_table_safe("cb_meetings", s)
    if meet is None:
        return {"error": "cb_meetings missing (run cb_rates first)"}
    model: ToneModel | None = None
    model_status = "disabled"
    if use_model:
        try:
            model = ToneModel()
            model.load()
            model_status = "ok"
        except Exception as e:  # noqa: BLE001
            log.warning("cb_tone: model unavailable, lexicon only: %s", e)
            model, model_status = None, f"unavailable: {type(e).__name__}"
    cache = ScoreCache(s)
    rows, rep = [], {"model": model_status, "docs": {}, "failed": {}}
    boj_map: dict[date, str] = {}
    for y in range(since.year, today.year + 1):
        try:
            boj_map.update(boj_links(y, s))
        except Exception as e:  # noqa: BLE001
            rep["failed"][f"boj_index_{y}"] = str(e)[:120]
    for bank in BANKS:
        ms = meet.filter((pl.col("bank") == bank) & (pl.col("meeting_date") >= since) &
                         (pl.col("meeting_date") <= today)).sort("meeting_date")
        n_ok = 0
        for d, murl in ms.select("meeting_date", "url").iter_rows():
            url = boj_map.get(d) if bank == "boj" else doc_url(bank, d, murl)
            if not url:
                continue
            try:
                paras = fetch_doc(bank, url, s)
            except Exception as e:  # noqa: BLE001
                rep["failed"][f"{bank}:{d}"] = str(e)[:120]
                continue
            sents = sentences(paras)
            if len(sents) < 3:
                rep["failed"][f"{bank}:{d}"] = "no text"
                continue
            sc = score_doc(sents, model, cache)
            dis, dtxt = vote_split(paras) if bank in ("fed", "boe", "boj") else (None, None)
            rows.append({"bank": bank, "date": d, "doc_type": "summary_minutes" if bank == "boe" else "statement",
                         "url": url, **sc, "dissents": dis, "vote_text": dtxt,
                         "model_id": MODEL_ID if sc["tone_model"] is not None else None})
            n_ok += 1
        rep["docs"][bank] = n_ok
    cache.flush()
    schema = {"bank": pl.Utf8, "date": pl.Date, "doc_type": pl.Utf8, "url": pl.Utf8, "n_sentences": pl.Int32,
              "tone_lexicon": pl.Float64, "tone_model": pl.Float64, "hawkish_share": pl.Float64,
              "dovish_share": pl.Float64, "dissents": pl.Int32, "vote_text": pl.Utf8, "model_id": pl.Utf8}
    df = pl.DataFrame(rows, schema=schema).with_columns(pl.lit(METHOD).alias("method_version"),
                                                        pl.lit(now_utc()).alias("computed_at"))
    rep["rows"] = df.height
    if write_out:
        rep["written"] = write(df, "cb_tone", s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = build_tone(ctx.settings, use_model=bool(p.get("use_model", True)))
    update_state({"items": {"W3.6": "done"}, "last_runs": {"cb_tone": {
        "at": now_utc().isoformat(timespec="seconds"), "rows": rep.get("rows"), "model": rep.get("model"),
        "docs": rep.get("docs"), "n_failed": len(rep.get("failed", {}))}}}, ctx.settings)
    return JobResult(status="failed" if "error" in rep else ("success" if rep.get("model") == "ok" else "partial"),
                     rows_written={"cb_tone": rep.get("written", 0)}, entities_ok=len(rep.get("docs", {})),
                     entities_failed=len(rep.get("failed", {})), message=json.dumps(rep, default=str)[:2000])
