"""SEC filing history per CIK from the EDGAR submissions API (data.sec.gov/submissions/CIK##########.json).

The `recent` block holds the latest ~1000 filings with form, filingDate, acceptanceDateTime (labelled UTC but late by the
US/Eastern offset once settled - corrected here, app.ingestion.sec_acceptance), reportDate,
items (8-K item numbers) and primaryDocument; older filings live in `files[]` pages (fetched on request and kept in
the HTTP cache forever: they never change). Raw JSON is landed by app.ingestion.sec_index.SecAdapter under
data/raw/sec_edgar/submissions/dt=YYYY-MM-DD/CIK##########.gz; this module reads the newest landing per CIK and only
re-requests the CIKs asked for (nightly: CIKs with a new 8-K / periodic report in filings_index).
"""

from __future__ import annotations

import gzip
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.earnings import common as C

log = C.log

PERIODIC = {"10-K": "annual", "10-K405": "annual", "10-KT": "annual", "20-F": "annual", "40-F": "annual",
            "10-Q": "quarterly", "10-QT": "quarterly"}
KEEP_FORMS = set(PERIODIC) | {"8-K", "DEF 14A"}
SCHEMA = {"cik": pl.Utf8, "accession": pl.Utf8, "form": pl.Utf8, "filing_date": pl.Date,
          "acceptance_ts": pl.Datetime("us", "UTC"), "report_date": pl.Date, "items": pl.List(pl.Utf8),
          "url": pl.Utf8}


def cik10(x: Any) -> str:
    return str(int(str(x).strip())).zfill(10)


def latest_raw_index(settings: Settings | None = None) -> dict[str, Path]:
    """cik10 -> newest landed submissions file."""
    s = settings or get_settings()
    root = s.raw_dir / "sec_edgar" / "submissions"
    out: dict[str, Path] = {}
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("dt=")):
        for f in d.iterdir():
            if f.name.startswith("CIK"):
                out[f.name[3:13]] = f
    return out


def landing_date(path: Path) -> date | None:
    """data/raw/sec_edgar/submissions/dt=YYYY-MM-DD/CIK...gz -> the landing (fetch) day."""
    name = path.parent.name
    try:
        return date.fromisoformat(name[3:13]) if name.startswith("dt=") else None
    except ValueError:
        return None


def _load(path: Path) -> dict | None:
    try:
        b = path.read_bytes()
        if b[:2] == b"\x1f\x8b":
            b = gzip.decompress(b)
        return orjson.loads(b)
    except (OSError, ValueError) as e:
        log.warning("unreadable submissions file %s: %s", path, e)
        return None


def _rows(block: dict, cik: str, fetched_on: date | None = None,
          truth: dict[str, datetime] | None = None) -> list[dict[str, Any]]:
    """`fetched_on`: day the submissions JSON was fetched (landing dt / today); None = long settled (older pages).
    acceptanceDateTime is corrected for EDGAR's offset bug (app.ingestion.sec_acceptance)."""
    from app.ingestion.sec_acceptance import block_vote, correct_acceptance  # noqa: PLC0415

    late = block_vote(block)   # is this file late by the ET offset? (per file, not per filing)
    forms = block.get("form") or []
    out = []
    g = block.get
    for i, f in enumerate(forms):
        if f not in KEEP_FORMS:  # amendments (".../A") are not releases
            continue
        acc = g("accessionNumber")[i]
        ts = g("acceptanceDateTime")[i] if g("acceptanceDateTime") else None
        fd = g("filingDate")[i]
        acc_ts = correct_acceptance(ts, f, date.fromisoformat(fd) if fd else None, fetched_on,
                                    (truth or {}).get(acc), late)[0] if ts else None
        rd = (g("reportDate") or [None] * len(forms))[i]
        items = (g("items") or [""] * len(forms))[i] or ""
        doc = (g("primaryDocument") or [""] * len(forms))[i] or ""
        nod = acc.replace("-", "")
        out.append({"cik": cik, "accession": acc, "form": f,
                    "filing_date": date.fromisoformat(fd) if fd else None, "acceptance_ts": acc_ts,
                    "report_date": date.fromisoformat(rd) if rd else None,
                    "items": [x.strip() for x in items.split(",") if x.strip()] or None,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nod}/{doc}" if doc else
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nod}/{acc}-index.htm"})
    return out


def parse_submissions(doc: dict, cik: str, older: list[dict] | None = None, fetched_on: date | None = None,
                      truth: dict[str, datetime] | None = None) -> list[dict[str, Any]]:
    rows = _rows((doc.get("filings") or {}).get("recent") or {}, cik, fetched_on, truth)
    for o in older or []:
        rows += _rows(o, cik, None, truth)
    return rows


def load_history(ciks: Iterable[str], refresh: Iterable[str] = (), fetch_missing: bool = False,
                 older_pages: bool = False, min_year: int = 2012, settings: Settings | None = None,
                 max_requests: int = 20000) -> tuple[pl.DataFrame, dict[str, int]]:
    """Filing rows (SCHEMA) for `ciks`. CIKs in `refresh` (and missing ones when fetch_missing) are requested from
    SEC (shared 8 req/s limiter); older pages are added when older_pages and the recent block starts after
    min_year."""
    s = settings or get_settings()
    want = sorted({cik10(c) for c in ciks if c})
    refresh_set = {cik10(c) for c in refresh}
    idx = latest_raw_index(s)
    ad = None
    stats = {"ciks": len(want), "from_raw": 0, "fetched": 0, "missing": 0, "older_pages": 0, "errors": 0}
    rows: list[dict[str, Any]] = []
    n_req = 0

    def adapter():
        nonlocal ad
        if ad is None:
            from app.ingestion.sec_index import SecIndexAdapter  # noqa: PLC0415 - concrete SecAdapter (shared limiter)

            ad = SecIndexAdapter(s)
        return ad

    try:
        from app.ingestion.sec_index import live_truth  # noqa: PLC0415

        truth = live_truth(s)
    except Exception:  # noqa: BLE001
        truth = {}
    for c in want:
        doc = None
        fetched_on: date | None = None
        need = c in refresh_set or (fetch_missing and c not in idx)
        if need and n_req < max_requests:
            try:
                doc = adapter().submissions(c, ttl=3 * 3600)
                fetched_on = C.now_utc().date()
                n_req += 1
                stats["fetched"] += 1
            except Exception as e:  # noqa: BLE001 - one CIK must not stop the run
                stats["errors"] += 1
                log.warning("submissions %s failed: %s", c, e)
        if doc is None and c in idx:
            doc = _load(idx[c])
            fetched_on = landing_date(idx[c])
            stats["from_raw"] += doc is not None
        if not doc:
            stats["missing"] += 1
            continue
        older: list[dict] = []
        if older_pages:
            recent = (doc.get("filings") or {}).get("recent") or {}
            fds = [x for x in (recent.get("filingDate") or []) if x]
            if fds and min(fds) > f"{min_year}-01-01":
                for f in (doc.get("filings") or {}).get("files") or []:
                    if (f.get("filingTo") or "") < f"{min_year}-01-01" or n_req >= max_requests:
                        continue
                    try:
                        r = adapter().http.get(f"https://data.sec.gov/submissions/{f['name']}", cache_ttl=-1)
                        n_req += 0 if r.from_cache else 1
                        older.append(r.json())
                        stats["older_pages"] += 1
                    except Exception as e:  # noqa: BLE001
                        stats["errors"] += 1
                        log.warning("submissions page %s failed: %s", f.get("name"), e)
        rows += parse_submissions(doc, c, older, fetched_on, truth)
    df = pl.DataFrame(rows, schema=SCHEMA) if rows else pl.DataFrame(schema=SCHEMA)
    df = df.filter(pl.col("filing_date") >= date(min_year, 1, 1)).unique("accession", keep="first")
    return df, stats


def recent_filers(days: int = 4, settings: Settings | None = None) -> list[str]:
    """CIKs (source_entity_id) with an 8-K / periodic report / proxy in filings_index over the last `days` days."""
    from app.core.storage import scan_table  # noqa: PLC0415

    s = settings or get_settings()
    since = C.now_utc().date() - timedelta(days=days)
    try:
        fi = (scan_table("filings_index", s)
              .filter((pl.col("source_id") == "sec_edgar") & (pl.col("knowledge_date") >= since)
                      & pl.col("form_class").is_in(["annual", "quarterly", "current_report", "proxy"]))
              .select("source_entity_id").unique().collect())
    except Exception:  # noqa: BLE001
        return []
    return [cik10(x) for x in fi.get_column("source_entity_id").to_list() if x and str(x).strip().isdigit()]
