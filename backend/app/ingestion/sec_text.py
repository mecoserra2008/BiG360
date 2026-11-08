"""SEC filing text (job `sec_text`): 10-K / 10-Q / 20-F sections and 8-K press releases -> compressed text files.

Selection (universe CIKs):
  * nightly: filings_index rows with knowledge_date > watermark and form in params.forms (default 10-K, 10-Q, 20-F,
    8-K and their amendments; 8-K only with item 2.02/7.01/8.01, i.e. results / Reg FD / other events).
  * params.latest_n (e.g. 1) + params.forms: the latest N filings per CIK found via the submissions API (used for
    backfills such as "latest 10-K of every universe company").

Output (feeds Lazy Prices + filing sentiment):
  data/raw/sec_text/<cik10>/<accession>/<section>.txt.gz   sections: full, business, risk_factors, mdna, legal,
                                                           press_release (EX-99.x of 8-Ks)
  data/raw/sec_text/_index.parquet                         one row per (filing_id, section): cik, form, filed,
                                                           period_end, path, n_chars, n_words, sha1, source_url
Original HTML is kept in the HTTP cache (immutable docs, TTL forever); filings_index.doc_path points to full.txt.gz
(relative to data/).

Section extraction: the HTML is flattened to text with one block element per line; headings are matched only at the
start of a line (cross-references inside paragraphs are ignored) and, among candidate start headings, the one that
yields the LONGEST span up to the next end heading wins (skips the table of contents).
"""

from __future__ import annotations

import gzip
import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import lxml.html
import polars as pl
from lxml import etree

from app.core.http import HttpError
from app.ingestion.base import FetchResult, register_adapter
from app.ingestion.sec_index import (
    ET,
    SEC_SOURCE,
    SecAdapter,
    base_form,
    cik10,
    doc_url,
    filing_index_url,
    filings_frame,
    form_class,
    is_amendment,
    make_filing_id,
    parse_acceptance_utc,
    parse_since,
    read_existing_filings,
    submissions_recent_by_acc,
    to_date,
)

DEFAULT_FORMS = ("10-K", "10-Q", "20-F", "8-K")
PR_ITEMS = {"2.02", "7.01", "8.01"}
BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article",
              "header", "footer", "ul", "ol", "dd", "dt", "blockquote", "pre", "center", "hr", "title"}
CELL_TAGS = {"td", "th"}

# ----------------------------------------------------------------------------------------------- HTML -> text

_SEP = r"[\s\.\:\-–— ]*"


def html_to_text(html: bytes | str) -> str:
    """Robust HTML/iXBRL -> plain text: drops scripts/styles/hidden iXBRL header, one block per line."""
    if isinstance(html, str):
        html = html.encode("utf-8", errors="replace")
    if not html.strip():
        return ""
    try:
        root = lxml.html.fromstring(html)
    except (etree.ParserError, ValueError):
        return re.sub(r"<[^>]+>", " ", html.decode("utf-8", errors="replace"))
    for bad in root.xpath("//script|//style|//head"):
        bad.drop_tree()
    for bad in [el for el in root.iter() if isinstance(el.tag, str) and el.tag.lower() in ("ix:header", "header")
                and el.tag.lower() == "ix:header"]:
        bad.drop_tree()
    for el in root.xpath("//*[@style]"):
        st = (el.get("style") or "").replace(" ", "").lower()
        if "display:none" in st:
            el.drop_tree()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.split("}")[-1].lower()
        if tag in BLOCK_TAGS:
            el.tail = "\n" + (el.tail or "")
            if tag in ("br", "hr"):
                continue
            el.text = "\n" + (el.text or "")
        elif tag in CELL_TAGS:
            el.tail = " \t" + (el.tail or "")
    text = root.text_content()
    text = text.replace(" ", " ").replace("​", "").replace("\r", "")
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _h(item: str, title: str) -> str:
    """Heading regex anchored at line start: 'Item 1A. Risk Factors' with flexible punctuation/spacing."""
    return rf"(?im)^[ \t]*(?:part[ \t]+i+[ \t]*[,\-–—]?[ \t]*)?item{_SEP}{item}{_SEP}(?:\(?[a-z]\)?\s*)?{title}"


SECTION_RULES: dict[str, dict[str, tuple[str, list[str]]]] = {
    "10-K": {
        "business": (_h("1", r"business"), [_h("1a", r"risk"), _h("1b", ""), _h("2", r"propert")]),
        "risk_factors": (_h("1a", r"risk\s*factors"), [_h("1b", ""), _h("1c", ""), _h("2", r"propert")]),
        "legal": (_h("3", r"legal\s*proceedings"), [_h("4", ""), rf"(?im)^[ \t]*part\s+ii\b"]),
        "mdna": (_h("7", r"management['’]?s?\s*discussion"), [_h("7a", ""), _h("8", r"financial\s*statements")]),
    },
    "10-Q": {
        "mdna": (_h("2", r"management['’]?s?\s*discussion"), [_h("3", r"quantitative"), _h("4", r"controls")]),
        "risk_factors": (_h("1a", r"risk\s*factors"), [_h("2", r"unregistered"), _h("3", r"defaults"), _h("5", ""),
                                                       _h("6", r"exhibits")]),
        "legal": (_h("1", r"legal\s*proceedings"), [_h("1a", r"risk"), _h("2", r"unregistered")]),
    },
    "20-F": {
        "risk_factors": (rf"(?im)^[ \t]*(?:item\s*3\.?\s*)?d{_SEP}risk\s*factors", [_h("4", r"information"),
                                                                               _h("4a", "")]),
        "business": (_h("4", r"information\s*on\s*the\s*company"), [_h("4a", ""), _h("5", r"operating")]),
        "mdna": (_h("5", r"operating\s*and\s*financial\s*review"), [_h("6", r"directors")]),
        "legal": (rf"(?im)^[ \t]*legal\s*(?:and\s*arbitration\s*)?proceedings\s*$", [rf"(?im)^[ \t]*[a-z]\.\s+\w"]),
    },
}
SECTION_RULES["10-K405"] = SECTION_RULES["10-KT"] = SECTION_RULES["10-K"]
SECTION_RULES["10-QT"] = SECTION_RULES["10-Q"]
MIN_SECTION_CHARS = {"business": 3000, "risk_factors": 3000, "mdna": 3000, "legal": 300}
MAX_SECTION_SHARE = 0.6  # a "section" longer than 60% of the document is a mis-detection

# MD&A inside an annual-report exhibit (EX-13, e.g. banks such as JPM incorporate Item 7 by reference)
EX13_MDNA = (r"(?im)^[ \t]*management['’]?s\s+discussion\s+and\s+analysis",
             [r"(?im)^[ \t]*(?:report\s+of\s+independent\s+registered|consolidated\s+(?:statements?|balance)|"
              r"management['’]?s\s+report\s+on\s+internal\s+control)"])


def extract_section(text: str, start_pat: str, end_pats: list[str], min_chars: int = 500) -> str | None:
    """Longest plausible span from a start heading to the first following end heading (or the next occurrence of the
    same start heading, so a table-of-contents entry never swallows the real section). Spans shorter than min_chars or
    longer than MAX_SECTION_SHARE of the document are rejected."""
    starts = [m.start() for m in re.finditer(start_pat, text)]
    if not starts:
        return None
    ends_all = sorted({m.start() for p in end_pats for m in re.finditer(p, text)} | set(starts))
    best = None
    for s in starts:
        e = next((x for x in ends_all if x > s + 20), None)
        if e is None:
            e = min(len(text), s + 400_000)
        span = text[s:e].strip()
        if len(span) < min_chars or len(span) > MAX_SECTION_SHARE * len(text):
            continue
        if best is None or len(span) > len(best):
            best = span
    return best


def extract_sections(text: str, form: str) -> dict[str, str]:
    out = {}
    for name, (sp, eps) in SECTION_RULES.get(base_form(form), {}).items():
        sec = extract_section(text, sp, eps, MIN_SECTION_CHARS.get(name, 500))
        if sec:
            out[name] = sec
    return out


def exhibit_docs(index_html: bytes | str, prefixes: tuple[str, ...] = ("EX-99",)) -> list[tuple[str, str]]:
    """Parse '<acc>-index.htm' -> [(type, href)] for documents whose Type starts with a prefix."""
    root = lxml.html.fromstring(index_html if isinstance(index_html, bytes) else index_html.encode())
    out = []
    for tr in root.xpath("//table[contains(@class,'tableFile')]//tr"):
        tds = tr.xpath("./td")
        if len(tds) < 4:
            continue
        typ = tds[3].text_content().strip().upper()
        a = tds[2].xpath(".//a/@href")
        if a and typ.startswith(prefixes):
            href = a[0]
            if "ix?doc=" in href:
                href = href.split("ix?doc=")[-1]
            out.append((typ, href if href.startswith("http") else "https://www.sec.gov" + href))
    return out


# ----------------------------------------------------------------------------------------------- adapter


INDEX_SCHEMA = {"filing_id": pl.Utf8, "section": pl.Utf8, "cik": pl.Utf8, "form_type": pl.Utf8,
                "knowledge_date": pl.Date, "period_end": pl.Date, "path": pl.Utf8, "n_chars": pl.Int64,
                "n_words": pl.Int64, "sha1": pl.Utf8, "source_url": pl.Utf8, "extracted_at": pl.Datetime("us", "UTC")}


@register_adapter
class SecTextAdapter(SecAdapter):
    source_id = "sec_text"
    produces = ("filings_index",)

    def __init__(self, *a: Any, params: dict[str, Any] | None = None, **kw: Any):
        super().__init__(*a, **kw)
        self.params = params or {}

    @property
    def text_root(self) -> Path:
        return self.settings.raw_dir / "sec_text"

    # -- selection --------------------------------------------------------------------------------------------------
    def _forms(self) -> set[str]:
        return {f.upper() for f in self.params.get("forms") or DEFAULT_FORMS}

    def _targets_from_index(self, since: date, universe: list[str]) -> list[dict]:
        fi = read_existing_filings(self.settings, filters=(pl.col("knowledge_date") > since)
                                   & pl.col("source_entity_id").is_in(universe))
        forms = self._forms()
        out = []
        for r in fi.to_dicts():
            b = base_form(r["form_type"])
            if b not in forms:
                continue
            if b == "8-K" and not (set(r.get("items") or []) & PR_ITEMS):
                continue
            out.append(r)
        return out

    def _targets_latest(self, ciks: list[str], n: int) -> list[dict]:
        forms = self._forms()
        out = []
        for c in ciks:
            s = self.submissions(c, ttl=24 * 3600)
            if not s:
                continue
            recs = sorted(submissions_recent_by_acc(s).items(), key=lambda kv: kv[1].get("filingDate", ""), reverse=True)
            per_form: dict[str, int] = {}
            for acc, r in recs:
                b = base_form(r.get("form", ""))
                if b not in forms or is_amendment(r.get("form", "")) or per_form.get(b, 0) >= n:
                    continue
                if b == "8-K" and not ({i.strip() for i in (r.get("items") or "").split(",")} & PR_ITEMS):
                    continue
                per_form[b] = per_form.get(b, 0) + 1
                filed = to_date(r.get("filingDate"))
                out.append({
                    "filing_id": make_filing_id(acc), "source_id": SEC_SOURCE, "year": filed.year,
                    "source_entity_id": c, "issuer_id": None, "form_type": r.get("form"),
                    "form_class": form_class(r.get("form", "")), "period_end": to_date(r.get("reportDate")),
                    "fiscal_year": None, "fiscal_period": None, "knowledge_date": filed,
                    "knowledge_ts": parse_acceptance_utc(r.get("acceptanceDateTime"), r.get("form"), filed,
                                                         datetime.now(UTC).date(), None, r.get("_file_late")),
                    "is_amendment": False, "amends_filing_id": None, "language": "en",
                    "url": doc_url(c, acc, r["primaryDocument"]) if r.get("primaryDocument") else filing_index_url(c, acc),
                    "doc_path": None, "has_xbrl": bool(r.get("isXBRL")),
                    "items": [i.strip() for i in (r.get("items") or "").split(",") if i.strip()] or None,
                    "ingested_at": datetime.now(UTC),
                })
        return out

    # -- fetch ----------------------------------------------------------------------------------------------------
    def _write_text(self, cik: str, acc: str, section: str, text: str) -> tuple[Path, str]:
        d = self.text_root / cik / acc
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{section}.txt.gz"
        data = text.encode("utf-8")
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(data, compresslevel=6))
        tmp.replace(p)
        return p, hashlib.sha1(data).hexdigest()

    def _process(self, rec: dict) -> tuple[list[dict], dict]:
        cik = rec["source_entity_id"]
        acc = rec["filing_id"].split(":", 1)[1]
        form = rec["form_type"]
        url = rec.get("url") or filing_index_url(cik, acc)
        if url.endswith("-index.htm"):  # primary document unknown -> resolve from the filing index page
            idx = self.http.get(url, cache_ttl=-1).content
            docs = exhibit_docs(idx, (base_form(form),))
            if not docs:
                raise ValueError("primary document not found in filing index")
            url = docs[0][1]
            rec["url"] = url
        html = self.http.get(url, cache_ttl=-1).content
        text = html_to_text(html)
        sections = {"full": text}
        sections.update(extract_sections(text, form))
        if base_form(form) in ("10-K", "10-KT", "10-Q", "10-QT") and "mdna" not in sections:
            # MD&A without an 'Item 7' / 'Item 2' heading (e.g. JPM: annual-report layout inside the 10-K/10-Q body)
            md = extract_section(text, EX13_MDNA[0], EX13_MDNA[1], MIN_SECTION_CHARS["mdna"])
            if md:
                sections["mdna"] = md
        if base_form(form) in ("10-K", "10-KT") and "mdna" not in sections:
            idx = self.http.get(filing_index_url(cik, acc), cache_ttl=-1).content
            for typ, href in exhibit_docs(idx, ("EX-13",)):
                if href.lower().endswith((".htm", ".html", ".txt")):
                    ex = html_to_text(self.http.get(href, cache_ttl=-1).content)
                    md = extract_section(ex, EX13_MDNA[0], EX13_MDNA[1], MIN_SECTION_CHARS["mdna"])
                    if md and len(md) <= MAX_SECTION_SHARE * len(ex) + 1:
                        sections["mdna"] = md
                        break
        if base_form(form) in ("8-K", "6-K"):
            idx = self.http.get(filing_index_url(cik, acc), cache_ttl=-1).content
            prs = []
            for typ, href in exhibit_docs(idx, ("EX-99",)):
                if href.lower().endswith((".htm", ".html", ".txt")):
                    prs.append(html_to_text(self.http.get(href, cache_ttl=-1).content))
            if prs:
                sections["press_release"] = "\n\n".join(prs)
        rows = []
        for name, txt in sections.items():
            p, sha = self._write_text(cik, acc, name, txt)
            rows.append({"filing_id": rec["filing_id"], "section": name, "cik": cik, "form_type": form,
                         "knowledge_date": rec["knowledge_date"], "period_end": rec.get("period_end"),
                         "path": p.relative_to(self.settings.data_dir).as_posix(), "n_chars": len(txt),
                         "n_words": len(txt.split()), "sha1": sha, "source_url": url,
                         "extracted_at": datetime.now(UTC)})
        rec["doc_path"] = (self.text_root / cik / acc / "full.txt.gz").relative_to(self.settings.data_dir).as_posix()
        return rows, rec

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(source_id=SEC_SOURCE, started_at=datetime.now(UTC))
        ciks = sorted({cik10(c) for c in entities}) if entities else self.universe_ciks(self.params)
        if self.params.get("latest_n"):
            targets = self._targets_latest(ciks, int(self.params["latest_n"]))
            res.watermark = None
        else:
            s = parse_since(since) or (datetime.now(ET).date() - timedelta(days=int(self.params.get("lookback_days", 7))))
            targets = self._targets_from_index(s, ciks)
            all_new = read_existing_filings(self.settings, filters=pl.col("knowledge_date") > s)
            res.watermark = (all_new["knowledge_date"].max() if all_new.height else s).isoformat()
        if self.params.get("skip_done", True):
            done = self._done_ids()
            targets = [t for t in targets if t["filing_id"] not in done]
        index_rows: list[dict] = []
        filings: list[dict] = []
        for rec in targets:
            acc = rec["filing_id"]
            try:
                rows, rec2 = self._process(rec)
                index_rows.extend(rows)
                filings.append(rec2)
                res.entities_ok.append(acc)
            except HttpError as e:
                res.entities_failed[acc] = str(e)[:300]
            except Exception as e:  # noqa: BLE001
                res.entities_failed[acc] = f"{type(e).__name__}: {str(e)[:250]}"
        res.meta["index_rows"] = index_rows
        res.meta["filings"] = filings
        res.meta["n_targets"] = len(targets)
        return res

    def _index_path(self) -> Path:
        return self.text_root / "_index.parquet"

    def _done_ids(self) -> set[str]:
        p = self._index_path()
        if not p.exists():
            return set()
        return set(pl.read_parquet(p, columns=["filing_id"])["filing_id"].to_list())

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        rows = fetched.meta.get("index_rows") or []
        if rows:
            new = pl.DataFrame(rows, schema=INDEX_SCHEMA, orient="row")
            p = self._index_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                old = pl.read_parquet(p)
                new = pl.concat([old, new], how="vertical_relaxed").unique(["filing_id", "section"], keep="last",
                                                                           maintain_order=True)
            tmp = p.with_suffix(".tmp")
            new.write_parquet(tmp)
            tmp.replace(p)
        filings = fetched.meta.get("filings") or []
        if not filings:
            return {"filings_index": filings_frame([])}
        ids = [f["filing_id"] for f in filings]
        prev = {r["filing_id"]: r for r in read_existing_filings(self.settings, ids).to_dicts()}
        out = []
        for f in filings:
            base = prev.get(f["filing_id"]) or f
            base = dict(base)
            base["doc_path"] = f["doc_path"]
            base["url"] = f.get("url") or base.get("url")
            out.append(base)
        return {"filings_index": filings_frame(out)}


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    ad = SecTextAdapter(settings=ctx.settings, params=dict(ctx.params))
    latest = bool(ctx.params.get("latest_n"))
    since = None if latest else (ctx.params.get("since") or ctx.watermark or ad.load_state().get("watermark"))
    fetched, rows = ad.run(since=since, entities=ctx.params.get("ciks"))
    if not latest and fetched.status != "failed" and fetched.watermark:
        ad.save_state(watermark=fetched.watermark)
    return JobResult(status=fetched.status, rows_written=rows, entities_ok=len(fetched.entities_ok),
                     entities_failed=len(fetched.entities_failed), watermark=None if latest else fetched.watermark,
                     http_requests=ad.http.n_requests, cache_hits=ad.http.n_cache_hits,
                     message=f"targets={fetched.meta.get('n_targets')} sections={len(fetched.meta.get('index_rows') or [])}")

