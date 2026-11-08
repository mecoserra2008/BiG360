"""ADR / GDR ratios from SEC cover pages (coverage v3 A6). Owner: ref-identity. Job `adr_ratios_sec` (manual DAG).

269 of the 270 valued ADRs had no ratio (seeds/adr_ratios.csv covers 64 curated names), so valuation divided their
underlying-share counts by 1: TLK (100 Series B shares per ADS) showed USD 1.1 tn, AKTX (2,000 ordinary per ADS)
USD 1.2 tn. The ratio is official and public: a 20-F / 40-F cover page registers the depositary shares as
"American Depositary Shares, each representing 100 Series B shares" (dei:Security12bTitle, rendered in the cover
report R1.htm of the filing).

    candidates()     active ADR / GDR securities without a ratio, with the issuer's CIK (identifier_map) and its latest
                     20-F / 40-F accession (fundamentals_raw sec_edgar / sec_fsds form types, else filings_index)
    fetch_cover()    one small GET per filing: R1.htm, else the 'Cover' report named in FilingSummary.xml (SEC
                     Archives, allowed by sec.gov robots.txt; app.core.http sec_edgar client = declared User-Agent,
                     8 req/s token bucket, immutable cache)
    parse_ratio()    'each representing N <class> shares', 'every N ADSs represent M shares', fractions and number words
    run / main       -> data/reference/seeds/adr_ratios_sec.csv (generated; seeds/adr_ratios.csv entries win, see
                     app.normalize.identity_universe.load_adr_ratios) + data/logs/reports/adr_ratios_sec.json (misses
                     with reason codes). The next identity_resolve / ref_universe sets securities_master.adr_ratio.

Point in time: the ratio is the one registered at the latest 20-F (an approximation for history before a ratio change;
valuation uses one ratio per ADR for every date).

    python -m app.ingestion.adr_ratios_sec --limit 25 --out <dir>      # sample / dry run (no seed write)
    python -m app.ingestion.adr_ratios_sec                             # all candidates -> seeds/adr_ratios_sec.csv
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

OUT_FILE = "adr_ratios_sec.csv"
REPORT_FILE = "adr_ratios_sec.json"
FORMS = ("20-F", "40-F", "20-F/A", "40-F/A", "10-K", "10-K/A")  # ADS issuers that became domestic filers (AKTX) state it on the 10-K cover
RATIO_MIN, RATIO_MAX = 1e-4, 1e5

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
          "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_FRACTIONS = {"half": 2, "halves": 2, "third": 3, "thirds": 3, "quarter": 4, "quarters": 4, "fourth": 4, "fourths": 4,
              "fifth": 5, "fifths": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "tenths": 10,
              "twentieth": 20, "twentieths": 20, "fortieth": 40, "fiftieth": 50, "hundredth": 100,
              "hundredths": 100, "thousandth": 1000}
_DR = r"(?:american\s+depositary|global\s+depositary|depositary\s+(?:shares?|receipts?)|\bads(?:s)?\b|\badr(?:s)?\b|\bgds(?:s)?\b|\bgdr(?:s)?\b)"
_SHARE_WORD = re.compile(r"\b(?:shares?|stock|units?|equity\s+shares?)\b")


def _words_to_number(tokens: list[str]) -> float | None:
    total, cur, seen = 0, 0, False
    for t in tokens:
        if t in _UNITS:
            cur += _UNITS[t]
            seen = True
        elif t == "hundred":
            cur = (cur or 1) * 100
            seen = True
        elif t == "thousand":
            total += (cur or 1) * 1000
            cur = 0
            seen = True
        elif t in ("and", "a", "an"):
            continue
        else:
            break
    return float(total + cur) if seen else None


def parse_number(span: str) -> float | None:
    """'2,000' / '0.5' / '1/10' / 'ten (10)' / 'twenty-five' / 'one-fifth of one' / 'one half of one' -> float."""
    s = span.lower().replace(",", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", s)
    if m and float(m.group(2)) > 0:
        return float(m.group(1)) / float(m.group(2))
    toks = [t for t in re.split(r"[\s\-]+", re.sub(r"[()]", " ", s)) if t]
    # fraction words: 'one-fifth of one ordinary share', 'two-thirds of a share', 'a half'
    for i, t in enumerate(toks):
        if t in _FRACTIONS:
            num = _words_to_number(toks[:i]) if i else 1.0
            if num is None and i and re.fullmatch(r"\d+(?:\.\d+)?", toks[i - 1]):
                num = float(toks[i - 1])
            return (num or 1.0) / _FRACTIONS[t]
    m = re.match(r"\(?(\d+(?:\.\d+)?)\)?", toks[0]) if toks else None
    if m:
        return float(m.group(1))
    w = _words_to_number(toks)
    if w is not None:
        # 'ten (10)': trust the digits in brackets when they follow the words
        d = re.search(r"\((\d+(?:\.\d+)?)\)", s)
        return float(d.group(1)) if d else w
    return None


def parse_ratio(text: str) -> tuple[float, str] | None:
    """Underlying shares per depositary share from cover-page text, with the matched phrase; None if not stated."""
    t = re.sub(r"\s+", " ", text.lower())
    for m in re.finditer(r"\brepresent(?:s|ing|ed)?\b", t):
        back = t[max(0, m.start() - 220): m.start()]
        if not re.search(_DR, back):
            continue
        tail = back[-90:]
        # how many depositary shares: 'each ADS', 'every five ADSs', 'every 10 of which', 'one ADS'
        q = re.search(r"\bevery\s+([\w\-]+(?:\s+[\w\-]+)?)\s+(?:of\s+which|(?:american\s+depositary\s+shares|ads(?:s)?|"
                      r"adr(?:s)?|gds(?:s)?|depositary\s+shares))", tail)
        per = parse_number(q.group(1)) if q else 1.0
        fwd = t[m.end(): m.end() + 120]
        sw = _SHARE_WORD.search(fwd)
        if not sw:
            continue
        span = fwd[: sw.start()]
        # drop the share-class words between the number and 'shares' ('100 series b', 'two class a ordinary')
        span = re.sub(r"\b(?:fully\s+paid|non-?voting|voting|restricted|registered|bearer|ordinary|common|preferred|"
                      r"preference|equity|class\s+\w+|series\s+\w+|of\s+(?:our|its|the|the\s+company's)|its|our)\b.*$",
                      "", span).strip()
        n = parse_number(span)
        if n is None or not per:
            continue
        r = n / per
        if RATIO_MIN <= r <= RATIO_MAX:
            phrase = t[max(0, m.start() - 80): m.end() + sw.end()].strip()
            return r, phrase[:240]
    return None


# ------------------------------------------------------------------------------------------------ candidates


def candidates(settings: Settings | None = None, tickers: Sequence[str] | None = None,
               include_known: bool = False) -> pl.DataFrame:
    """[security_id, issuer_id, display_ticker, cik, accession, filed, underlying] of active ADR / GDR securities
    (without a ratio unless include_known); accession null when no 20-F / 40-F is on disk."""
    from app.core.storage import scan_table  # noqa: PLC0415

    s = settings or get_settings()
    sm = scan_table("securities_master", s).filter(pl.col("security_type").is_in(["adr", "gdr"]) & pl.col("is_active"))
    if not include_known:
        sm = sm.filter(pl.col("adr_ratio").is_null())
    sm = sm.select("security_id", "issuer_id", "display_ticker", "underlying_security_id").collect()
    if tickers:
        sm = sm.filter(pl.col("display_ticker").is_in([t.upper() for t in tickers]))
    if sm.is_empty():
        return sm.with_columns(pl.lit(None, pl.Utf8).alias("cik"), pl.lit(None, pl.Utf8).alias("accession"),
                               pl.lit(None, pl.Date).alias("filed"))
    iss = sm.get_column("issuer_id").unique().to_list()
    cik = (scan_table("identifier_map", s).filter((pl.col("id_type") == "cik") & pl.col("issuer_id").is_in(iss))
           .select("issuer_id", pl.col("id_value").alias("cik")).unique(subset=["issuer_id"], keep="first").collect())
    fil = []
    try:
        fil.append(scan_table("fundamentals_raw", s)
                   .filter(pl.col("source_id").is_in(["sec_edgar", "sec_fsds"]) & pl.col("issuer_id").is_in(iss)
                           & pl.col("form_type").is_in(list(FORMS)))
                   .select("issuer_id", "filing_id", pl.col("knowledge_date").alias("filed")).unique().collect())
    except Exception as e:  # noqa: BLE001
        log.warning("adr_ratios_sec: fundamentals_raw unreadable (%s)", type(e).__name__)
    try:
        fil.append(scan_table("filings_index", s)
                   .filter((pl.col("source_id") == "sec_edgar") & pl.col("issuer_id").is_in(iss)
                           & pl.col("form_type").is_in(list(FORMS)))
                   .select("issuer_id", "filing_id", pl.col("knowledge_date").alias("filed")).unique().collect())
    except Exception as e:  # noqa: BLE001
        log.warning("adr_ratios_sec: filings_index unreadable (%s)", type(e).__name__)
    fl = (pl.concat(fil, how="vertical_relaxed") if fil else
          pl.DataFrame(schema={"issuer_id": pl.Utf8, "filing_id": pl.Utf8, "filed": pl.Date}))
    fl = (fl.filter(pl.col("filing_id").str.starts_with("sec_edgar:"))
          .with_columns(pl.col("filing_id").str.split(":").list.get(1).alias("accession"))
          .sort("issuer_id", "filed").group_by("issuer_id").agg(pl.col("accession").last(), pl.col("filed").last()))
    und = scan_table("securities_master", s).select(pl.col("security_id").alias("underlying_security_id"),
                                                     pl.col("display_ticker").alias("underlying")).collect()
    return (sm.join(cik, on="issuer_id", how="left").join(fl, on="issuer_id", how="left")
            .join(und, on="underlying_security_id", how="left").drop("underlying_security_id")
            .sort("display_ticker"))


# ------------------------------------------------------------------------------------------------ fetch


def _base(cik: str, acc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"


def cover_report_name(filing_summary_xml: bytes | str) -> str | None:
    """FilingSummary.xml -> HtmlFileName of the report whose ShortName mentions the cover page."""
    import lxml.etree as ET  # noqa: PLC0415

    raw = filing_summary_xml if isinstance(filing_summary_xml, bytes) else filing_summary_xml.encode()
    try:
        root = ET.fromstring(raw)
    except ET.XMLSyntaxError:
        return None
    for rep in root.iter("Report"):
        short = (rep.findtext("ShortName") or "").lower()
        if "cover" in short or "document and entity" in short:
            return rep.findtext("HtmlFileName")
    return None


def fetch_cover(http: Any, cik: str, acc: str) -> tuple[str | None, str]:
    """(cover text, url) or (None, reason) - one or two small GETs (cache forever: filed documents are immutable)."""
    from app.core.http import HttpError  # noqa: PLC0415
    from app.ingestion.sec_text import html_to_text  # noqa: PLC0415

    base = _base(cik, acc)
    try:
        r1 = http.get(f"{base}/R1.htm", cache_ttl=-1)
        txt = html_to_text(r1.content)
        if parse_ratio(txt) or "depositary" in txt.lower():
            return txt, f"{base}/R1.htm"
    except HttpError as e:
        if e.status not in (404, 403):
            return None, f"http_{e.status}"
    try:
        fs = http.get(f"{base}/FilingSummary.xml", cache_ttl=-1)
    except HttpError as e:
        return None, f"no_filing_summary_{e.status}"
    name = cover_report_name(fs.content)
    if name == "R1.htm":
        return None, "no_ratio_phrase"   # R1 is the cover and does not state the ratio
    if not name:
        return None, "no_cover_report"
    try:
        rr = http.get(f"{base}/{name}", cache_ttl=-1)
    except HttpError as e:
        return None, f"http_{e.status}"
    return html_to_text(rr.content), f"{base}/{name}"


# ------------------------------------------------------------------------------------------------ run


def extract(cands: pl.DataFrame, http: Any) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """-> (found [adr_ticker, adr_ratio, underlying, verified, note], misses [{ticker, reason}])."""
    found, misses = [], []
    for r in cands.iter_rows(named=True):
        tk = r["display_ticker"]
        if not r.get("cik"):
            misses.append({"ticker": tk, "reason": "no_cik"})
            continue
        if not r.get("accession"):
            misses.append({"ticker": tk, "reason": "no_20f_on_disk"})
            continue
        text, where = fetch_cover(http, r["cik"], r["accession"])
        if text is None:
            misses.append({"ticker": tk, "reason": where, "accession": r["accession"]})
            continue
        got = parse_ratio(text)
        if got is None:
            misses.append({"ticker": tk, "reason": "no_ratio_phrase", "accession": r["accession"], "url": where})
            continue
        ratio, phrase = got
        found.append({"adr_ticker": tk, "adr_ratio": f"{ratio:g}", "underlying": r.get("underlying") or "",
                      "verified": "sec_cover",
                      "note": f"{r['accession']} ({r['filed']}): {phrase}".replace("\n", " ")})
    cols = {"adr_ticker": pl.Utf8, "adr_ratio": pl.Utf8, "underlying": pl.Utf8, "verified": pl.Utf8, "note": pl.Utf8}
    return pl.DataFrame(found, schema=cols), misses


def output_path(settings: Settings) -> Path:
    from app.normalize.identity_universe import seeds_dir  # noqa: PLC0415

    return seeds_dir(settings) / OUT_FILE


def write_output(found: pl.DataFrame, misses: list[dict[str, Any]], out: Path, report: Path,
                 n_candidates: int) -> None:
    """Merge into the generated seed (a ticker found again replaces its row) + JSON report."""
    old = pl.read_csv(out, infer_schema_length=0, comment_prefix="#") if out.exists() else None
    merged = found if old is None or old.is_empty() else pl.concat(
        [old.filter(~pl.col("adr_ticker").is_in(found.get_column("adr_ticker").implode())), found], how="diagonal_relaxed")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.write("# GENERATED by app.ingestion.adr_ratios_sec from SEC 20-F / 40-F cover pages (coverage v3 A6). "
                "Do not edit: curated values go in adr_ratios.csv, which wins.\n")
        merged.sort("adr_ticker").write_csv(f)
    tmp.replace(out)
    report.parent.mkdir(parents=True, exist_ok=True)
    by: dict[str, int] = {}
    for m in misses:
        by[m["reason"]] = by.get(m["reason"], 0) + 1
    report.write_text(json.dumps({"at": datetime.now(UTC).isoformat(), "candidates": n_candidates,
                                  "found": found.height, "misses_by_reason": by, "misses": misses,
                                  "output": str(out)}, indent=1, default=str), encoding="utf-8")


def run_extract(settings: Settings | None = None, tickers: Sequence[str] | None = None, limit: int | None = None,
                out_dir: Path | None = None, http: Any = None) -> dict[str, Any]:
    from app.core.http import HttpClient  # noqa: PLC0415

    s = settings or get_settings()
    c = candidates(s, tickers)
    if limit:
        c = c.head(int(limit))
    http = http or HttpClient("sec_edgar")
    found, misses = extract(c, http)
    out = (out_dir / OUT_FILE) if out_dir else output_path(s)
    report = (out_dir / REPORT_FILE) if out_dir else (s.logs_dir / "reports" / REPORT_FILE)
    write_output(found, misses, out, report, c.height)
    return {"candidates": c.height, "found": found.height, "misses": len(misses), "output": str(out),
            "report": str(report)}


def run(ctx: Any) -> Any:
    """jobs.yaml `adr_ratios_sec` (manual): params tickers, limit."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    tk = p.get("tickers")
    if isinstance(tk, str):
        tk = [t.strip() for t in tk.split(",") if t.strip()]
    rep = run_extract(ctx.settings, tk, p.get("limit"))
    return JobResult(status="success", rows_written={"adr_ratios_sec.csv": rep["found"]}, entities_ok=rep["found"],
                     entities_failed=rep["misses"], message=json.dumps(rep)[:1500])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingestion.adr_ratios_sec")
    ap.add_argument("--tickers", help="comma-separated ADR tickers")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", help="write the CSV + report here instead of the seeds dir (dry run)")
    a = ap.parse_args(argv)
    tk = [t.strip() for t in a.tickers.split(",")] if a.tickers else None
    print(json.dumps(run_extract(get_settings(), tk, a.limit, Path(a.out) if a.out else None), indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
