"""Roadmap #9: compact store of link-candidate text units (job `linkage_text_units`) for app.linkage.textrules.

Only short candidate sentences / customer-table rows are kept (app.linkage.textrules.candidate_units), never the
filing HTML: data/raw/linkage/text_units/units_<stamp>.parquet
    filing_id, cik, form_type, knowledge_date, period_end, origin (sec_text | fetch), unit_no, text
State data/state/linkage_text_units.json {"done": {filing_id: n_units}, "failed": {filing_id: error}}.

Two inputs, both processed one filing at a time (bounded memory):
  1. sec_text   10-K / 20-F / 40-F full texts already landed by the `sec_text` job (raw/sec_text/_index.parquet)
  2. fetch      the latest annual report (10-K / 20-F / 40-F, filed >= params.since) of every CIK in the landed SEC
                submissions (raw/sec_edgar/submissions, one gz JSON per CIK) that is not in the sec_text corpus:
                primary document downloaded WITHOUT the HTTP cache (cache_ttl=0), flattened, reduced to units, dropped.
                20-F / 40-F (foreign private issuers) first, then 10-K of CIKs mapped to our issuers. Capped by
                params.max_fetch per run (resumable; a detached backfill uses a large cap).
"""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings
from app.linkage.common import load_state, log, now_utc, raw_dir, save_state, settings_of

STATE = "text_units"
ANNUAL_FORMS = ("10-K", "20-F", "40-F")
FLUSH_EVERY = 40


def units_dir(settings: Settings | None = None) -> Path:
    return raw_dir("text_units", settings)


def load_units(settings: Settings | None = None) -> pl.DataFrame:
    files = sorted(units_dir(settings).glob("units_*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed").unique(
        ["filing_id", "unit_no"], keep="last")


class _Sink:
    """Buffers unit rows and flushes a parquet part every FLUSH_EVERY filings (+ state)."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.rows: list[dict[str, Any]] = []
        self.done: dict[str, int] = {}
        self.failed: dict[str, str] = {}
        self.n = 0

    def add(self, meta: dict[str, Any], units: list[str]) -> None:
        for k, u in enumerate(units):
            self.rows.append({**meta, "unit_no": k, "text": u})
        self.done[meta["filing_id"]] = len(units)
        self.n += 1
        if self.n % FLUSH_EVERY == 0:
            self.flush()

    def fail(self, filing_id: str, err: str) -> None:
        self.failed[filing_id] = err[:200]

    def flush(self) -> None:
        if self.rows:
            df = pl.DataFrame(self.rows, schema={
                "filing_id": pl.Utf8, "cik": pl.Utf8, "form_type": pl.Utf8, "knowledge_date": pl.Date,
                "period_end": pl.Date, "origin": pl.Utf8, "unit_no": pl.Int32, "text": pl.Utf8})
            stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
            df.write_parquet(units_dir(self.s) / f"units_{stamp}.parquet", compression="zstd")
            self.rows = []
        if self.done or self.failed:
            st = load_state(STATE, self.s)
            st.setdefault("done", {}).update(self.done)
            st.setdefault("failed", {}).update(self.failed)
            for k in self.done:
                st["failed"].pop(k, None)
            st["updated"] = now_utc().isoformat(timespec="seconds")
            save_state(STATE, st, self.s)
            self.done, self.failed = {}, {}


# ================================================================================================ 1. sec_text corpus
def from_sec_text(settings: Settings | None = None, forms: tuple[str, ...] = ("10-K", "10-K/A", "20-F", "20-F/A",
                                                                              "40-F")) -> dict[str, Any]:
    from app.linkage.textrules import candidate_units  # noqa: PLC0415

    s = settings_of(settings)
    idx_p = s.raw_dir / "sec_text" / "_index.parquet"
    if not idx_p.exists():
        return {"sec_text_filings": 0}
    idx = pl.read_parquet(idx_p).filter((pl.col("section") == "full") & pl.col("form_type").is_in(list(forms)))
    done = set(load_state(STATE, s).get("done", {}))
    sink = _Sink(s)
    n = 0
    for r in idx.iter_rows(named=True):
        if r["filing_id"] in done:
            continue
        try:
            with gzip.open(s.data_dir / r["path"], "rt", encoding="utf-8", errors="ignore") as fh:
                txt = fh.read()
        except OSError as e:
            sink.fail(r["filing_id"], str(e))
            continue
        sink.add({"filing_id": r["filing_id"], "cik": str(r["cik"]).zfill(10), "form_type": r["form_type"],
                  "knowledge_date": r["knowledge_date"], "period_end": r["period_end"], "origin": "sec_text"},
                 candidate_units(txt))
        del txt
        n += 1
    sink.flush()
    return {"sec_text_filings": n}


# ================================================================================================ 2. fetch
def _latest_submissions(settings: Settings) -> dict[str, Path]:
    root = settings.raw_dir / "sec_edgar" / "submissions"
    out: dict[str, Path] = {}
    if not root.exists():
        return out
    for d in sorted(root.glob("dt=*")):           # later partitions overwrite earlier ones
        for f in d.glob("CIK*.gz"):
            out[f.name[3:13]] = f
    return out


def fetch_targets(settings: Settings | None = None, since: date = date(2023, 1, 1),
                  forms: tuple[str, ...] = ANNUAL_FORMS) -> list[dict[str, Any]]:
    """Latest annual report per CIK from the landed submissions, minus filings already in the corpus / unit store."""
    from app.ingestion.sec_index import base_form, doc_url, is_amendment, make_filing_id, submissions_recent_by_acc  # noqa: PLC0415
    from app.linkage.georev import issuer_ids  # noqa: PLC0415

    s = settings_of(settings)
    have: set[str] = set(load_state(STATE, s).get("done", {}))
    idx_p = s.raw_dir / "sec_text" / "_index.parquet"
    if idx_p.exists():
        have |= set(pl.read_parquet(idx_p, columns=["filing_id"])["filing_id"].to_list())
    ids = issuer_ids(s)
    mapped = set() if ids is None else set(ids["entity_id"].to_list())
    out = []
    for cik, path in _latest_submissions(s).items():
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                sub = json.load(fh)
        except (OSError, ValueError):
            continue
        best = None
        for acc, r in submissions_recent_by_acc(sub).items():
            f = r.get("form", "")
            if base_form(f) not in forms or is_amendment(f) or not r.get("primaryDocument"):
                continue
            fd = r.get("filingDate") or ""
            if fd < since.isoformat():
                continue
            if best is None or fd > best[1].get("filingDate", ""):
                best = (acc, r)
        if best is None:
            continue
        acc, r = best
        fid = make_filing_id(acc)
        if fid in have:
            continue
        pe = r.get("reportDate") or None
        out.append({"filing_id": fid, "cik": cik, "form_type": r["form"],
                    "knowledge_date": date.fromisoformat(r["filingDate"]),
                    "period_end": date.fromisoformat(pe) if pe else None,
                    "url": doc_url(cik, acc, r["primaryDocument"]), "mapped": cik in mapped,
                    "foreign": base_form(r["form"]) in ("20-F", "40-F")})
    # foreign private issuers first, then CIKs mapped to our issuers, newest first
    out.sort(key=lambda t: (not t["foreign"], not t["mapped"], -t["knowledge_date"].toordinal()))
    return out


def fetch(settings: Settings | None = None, max_fetch: int = 300, since: date = date(2023, 1, 1),
          rate_per_s: float = 3.0) -> dict[str, Any]:
    from app.core.http import HttpError  # noqa: PLC0415
    from app.core.ratelimit import RateLimit  # noqa: PLC0415
    from app.ingestion.sec_text import html_to_text  # noqa: PLC0415
    from app.linkage.common import http  # noqa: PLC0415
    from app.linkage.textrules import candidate_units  # noqa: PLC0415

    s = settings_of(settings)
    targets = fetch_targets(s, since)
    rep: dict[str, Any] = {"fetch_targets": len(targets), "fetched": 0, "fetch_failed": 0}
    if not targets or max_fetch <= 0:
        return rep
    cl = http("sec_edgar", s, rate_limit=RateLimit(rate_per_s, max(1, int(rate_per_s))))
    sink = _Sink(s)
    try:
        for t in targets[:max_fetch]:
            try:
                html = cl.get(t["url"], cache_ttl=0).content
                if len(html) > 60_000_000:
                    raise ValueError(f"document too large ({len(html)} bytes)")
                txt = html_to_text(html)
                del html
                units = candidate_units(txt)
                del txt
            except (HttpError, ValueError, OSError) as e:
                sink.fail(t["filing_id"], f"{type(e).__name__}: {e}")
                rep["fetch_failed"] += 1
                continue
            sink.add({k: t[k] for k in ("filing_id", "cik", "form_type", "knowledge_date", "period_end")}
                     | {"origin": "fetch"}, units)
            rep["fetched"] += 1
            if rep["fetched"] % 100 == 0:
                log.info("linkage text units: fetched %d / %d", rep["fetched"], min(max_fetch, len(targets)))
    finally:
        sink.flush()
        cl.close()
    return rep


def build_units(settings: Settings | None = None, max_fetch: int = 300, since: date = date(2023, 1, 1)) -> dict[str, Any]:
    rep = from_sec_text(settings)
    rep.update(fetch(settings, max_fetch=max_fetch, since=since))
    return rep


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = build_units(ctx.settings, max_fetch=int(p.get("max_fetch", 300)),
                      since=date.fromisoformat(str(p.get("since", "2023-01-01"))))
    return JobResult(status="success", rows_written={}, message=str(rep))


if __name__ == "__main__":  # detached backfill: python -m app.linkage.textunits [max_fetch]
    import sys

    from app.core.logsetup import setup_logging  # noqa: PLC0415

    try:
        setup_logging()
    except Exception:  # noqa: BLE001
        pass
    a = sys.argv[1:]
    print(build_units(max_fetch=int(a[0]) if a else 300))
