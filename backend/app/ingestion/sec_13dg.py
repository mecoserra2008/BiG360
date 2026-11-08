"""SEC Schedule 13D / 13G filings with BOTH parties (subject company + filer) -> activist_filings (roadmap #7; job
`activist_filings`, nightly; backfill via params since=YYYY-MM-DD).

filings_index keeps one CIK per accession (the subject issuer when it has a ticker), which loses the filer. The EDGAR
master index lists one line per CIK of a filing (subject company and every filer), so this job re-reads the master
index restricted to 13D / 13G forms:
  * backfill: quarterly `full-index/YYYY/QTRn/master.gz` (streamed to a temp file, parsed, deleted: no raw bloat);
  * incremental: `daily-index/YYYY/QTRn/master.YYYYMMDD.idx` after the watermark (lookback 7 days), using the raw
    daily files already landed by `sec_filings_index` when present.
Forms: SC 13D, SC 13D/A, SC 13G, SC 13G/A (until 2024-12) and SCHEDULE 13D(/A), SCHEDULE 13G(/A) (EDGAR XML schedules).

Role assignment per accession (subject vs filer), recorded in `role_source`:
  ticker_map      exactly one CIK is in the SEC company_tickers map or our identifier_map (cik) -> subject;
  header          ambiguous 13D filings: the filing header (-index-headers.html: SUBJECT COMPANY / FILED BY) is read,
                  at most params.max_header_fetches (default 400) per run;
  name_heuristic  remaining ambiguity: a CIK whose name looks like an investment manager (LP, LLC, capital, partners,
                  fund, management, advisors, ...) is a filer;
  unresolved      otherwise the first line is taken as subject (flagged).
knowledge_date = Date Filed (EDGAR), or the daily-index date when dissemination was delayed by > 5 days.
"""

from __future__ import annotations

import gzip
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.logsetup import get_logger
from app.core.schema import empty_frame
from app.ingestion.base import FetchResult, register_adapter
from app.ingestion.sec_index import (
    ARCHIVES,
    DISSEMINATION_LAG_DAYS,
    SEC_SOURCE,
    SecAdapter,
    acc_nodash,
    filing_index_url,
    make_filing_id,
    parse_master_idx,
    parse_since,
    quarter_of,
    to_date,
    try_lookup_many,
)

log = get_logger("app.ingestion.sec_13dg")

FORMS_13DG = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G",
              "SCHEDULE 13G/A"}
MANAGER_RX = re.compile(
    r"\b(L\.?P\.?|LLC|L\.L\.C\.|LLP|CAPITAL|PARTNERS|FUND|FUNDS|MANAGEMENT|ADVISORS?|ADVISERS?|INVESTMENTS?|"
    r"ASSET|HOLDINGS LP|MASTER|OPPORTUNIT(Y|IES)|TRUST CO|GROUP LP|VENTURES?|ACTIVIST|EQUITY PARTNERS)\b", re.IGNORECASE)


def schedule_of(form: str) -> str:
    return "13D" if "13D" in form.upper() else "13G"


def is_amend(form: str) -> bool:
    return form.strip().upper().endswith("/A")


def filter_13dg(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if e["form"].strip().upper() in FORMS_13DG]


def parse_header_roles(html: str) -> tuple[str | None, list[str]]:
    """-index-headers.html -> (subject CIK, [filer CIKs]) from the SUBJECT COMPANY / FILED BY blocks."""
    text = re.sub(r"<[^>]+>", " ", html)
    subj, filers = None, []
    for block in re.split(r"(?=SUBJECT COMPANY:|FILED BY:)", text):
        m = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", block)
        if not m:
            continue
        cik = m.group(1).zfill(10)
        if block.startswith("SUBJECT COMPANY:"):
            subj = subj or cik
        elif block.startswith("FILED BY:"):
            filers.append(cik)
    return subj, filers


def assign_roles(es: list[dict[str, Any]], known: set[str],
                 header: tuple[str | None, list[str]] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Lines of one accession -> (subject line, filer lines, role_source)."""
    uniq = list({e["cik"]: e for e in es}.values())
    if header and header[0] and any(e["cik"] == header[0] for e in uniq):
        subj = next(e for e in uniq if e["cik"] == header[0])
        return subj, [e for e in uniq if e is not subj], "header"
    k = [e for e in uniq if e["cik"] in known]
    if len(k) == 1 and len(uniq) > 1:
        return k[0], [e for e in uniq if e is not k[0]], "ticker_map"
    pool = k if len(k) > 1 else uniq
    non_mgr = [e for e in pool if not MANAGER_RX.search(e["name"] or "")]
    if len(pool) > 1 and len(non_mgr) == 1:
        s = non_mgr[0]
        return s, [e for e in uniq if e is not s], "name_heuristic"
    if len(uniq) == 1:  # a filing indexed under one CIK only (rare): treat it as the subject
        return uniq[0], [], "unresolved"
    return pool[0], [e for e in uniq if e is not pool[0]], "unresolved"


def needs_header(es: list[dict[str, Any]], known: set[str]) -> bool:
    uniq = {e["cik"] for e in es}
    return len(uniq) > 1 and len(uniq & known) != 1


def build_rows(entries: list[dict[str, Any]], known: set[str], headers: dict[str, tuple[str | None, list[str]]],
               now: datetime) -> pl.DataFrame:
    by_acc: dict[str, list[dict[str, Any]]] = {}
    for e in filter_13dg(entries):
        by_acc.setdefault(e["accession"], []).append(e)
    rows = []
    for acc, es in by_acc.items():
        subj, filers, src = assign_roles(es, known, headers.get(acc))
        filed = to_date(subj["filed"])
        if filed is None:
            continue
        idx_days = [e["index_date"] for e in es if e.get("index_date")]
        idx = min(idx_days) if idx_days else None
        kd = idx if idx is not None and (idx - filed).days > DISSEMINATION_LAG_DAYS else filed
        form = subj["form"].strip().upper()
        f0 = filers[0] if filers else None
        rows.append({
            "filing_id": make_filing_id(acc), "accession": acc, "form_type": form, "schedule": schedule_of(form),
            "is_amendment": is_amend(form), "filed_date": filed, "knowledge_date": kd, "year": kd.year,
            "subject_cik": subj["cik"], "subject_name": subj["name"],
            "filer_cik": f0["cik"] if f0 else None, "filer_name": f0["name"] if f0 else None,
            "other_filer_ciks": [f["cik"] for f in filers[1:]] or None, "n_filers": len(filers),
            "role_source": src, "issuer_id": None, "security_id": None,
            "url": filing_index_url(subj["cik"], acc), "source_id": SEC_SOURCE, "ingested_at": now,
        })
    if not rows:
        return empty_frame("activist_filings")
    df = pl.DataFrame(rows, infer_schema_length=None)
    m = try_lookup_many("cik", df["subject_cik"].unique().to_list())
    if m:
        iss = {k: v.get("issuer_id") for k, v in m.items()}
        sec = {k: v.get("primary_security_id") or v.get("security_id") for k, v in m.items()}
        df = df.with_columns(pl.col("subject_cik").replace_strict(iss, default=None).alias("issuer_id"),
                             pl.col("subject_cik").replace_strict(sec, default=None).alias("security_id"))
    return df


@register_adapter
class Sec13DGAdapter(SecAdapter):
    source_id = "sec_13dg"
    produces = ("activist_filings",)

    def __init__(self, *a: Any, params: dict[str, Any] | None = None, **kw: Any):
        super().__init__(*a, **kw)
        self.params = params or {}

    def _known_ciks(self) -> set[str]:
        known = set(self.ticker_map()["cik"].to_list()) if self.params.get("use_ticker_map", True) else set()
        try:
            from app.core.storage import scan_table  # noqa: PLC0415

            known |= set(scan_table("identifier_map", self.settings).filter(pl.col("id_type") == "cik")
                         .select("id_value").collect()["id_value"].to_list())
        except Exception as e:  # noqa: BLE001 - identity layer optional
            log.debug("identifier_map unavailable for 13D/G roles: %s", e)
        return known

    def _full_quarter(self, y: int, q: int) -> list[dict[str, Any]]:
        tmp = self.settings.data_dir / "tmp" / f"x7_master_{y}Q{q}_{uuid.uuid4().hex[:6]}.gz"
        try:
            self.http.download(f"{ARCHIVES}/full-index/{y}/QTR{q}/master.gz", tmp)
            with gzip.open(tmp, "rt", encoding="latin-1") as fh:
                ents = filter_13dg(parse_master_idx(fh.read(), None))
        finally:
            tmp.unlink(missing_ok=True)
        return ents

    def _daily(self, day: date, name: str) -> list[dict[str, Any]]:
        raw = self.settings.raw_dir / SEC_SOURCE / "daily_index" / f"dt={day.isoformat()}" / "master.idx.gz"
        if raw.exists():
            text = gzip.decompress(raw.read_bytes()).decode("latin-1")
        else:
            text = self.http.get(f"{ARCHIVES}/daily-index/{day.year}/QTR{quarter_of(day)}/{name}",
                                 cache_ttl=0).content.decode("latin-1")
        return filter_13dg(parse_master_idx(text, day))

    def _daily_listing(self, year: int, q: int) -> dict[date, str]:
        try:
            items = self.http.get(f"{ARCHIVES}/daily-index/{year}/QTR{q}/index.json", cache_ttl=3600).json()["directory"]["item"]
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        for it in items:
            m = re.match(r"master\.(\d{8})\.idx$", it.get("name", ""))
            if m:
                out[datetime.strptime(m.group(1), "%Y%m%d").date()] = it["name"]
        return out

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(source_id=SEC_SOURCE, started_at=datetime.now(UTC))
        today = parse_since(self.params.get("as_of")) or datetime.now(UTC).date()
        st = self.load_state()
        start = parse_since(since) or (parse_since(st.get("watermark")) or today - timedelta(days=30)) - timedelta(days=7)
        entries: list[dict[str, Any]] = []
        y, q = start.year, quarter_of(start)
        cy, cq = today.year, quarter_of(today)
        daily_from = start
        if self.params.get("full_index", (today - start).days > 100):
            while (y, q) < (cy, cq):
                try:
                    entries += self._full_quarter(y, q)
                    res.entities_ok.append(f"{y}Q{q}")
                except Exception as e:  # noqa: BLE001
                    res.entities_failed[f"{y}Q{q}"] = str(e)[:300]
                y, q = (y + 1, 1) if q == 4 else (y, q + 1)
            daily_from = date(cy, (cq - 1) * 3 + 1, 1) - timedelta(days=1)
        days: dict[date, str] = {}
        d = daily_from
        seen: set[tuple[int, int]] = set()
        while d <= today:
            k = (d.year, quarter_of(d))
            if k not in seen:
                seen.add(k)
                days.update(self._daily_listing(*k))
            d += timedelta(days=1)
        wm = daily_from
        for day in sorted(x for x in days if daily_from < x <= today):
            try:
                entries += self._daily(day, days[day])
                res.entities_ok.append(day.isoformat())
                wm = max(wm, day)
            except Exception as e:  # noqa: BLE001
                res.entities_failed[day.isoformat()] = str(e)[:300]
        res.watermark = wm.isoformat()
        res.meta["entries"] = entries
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        entries = fetched.meta.get("entries") or []
        known = self._known_ciks()
        by_acc: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            by_acc.setdefault(e["accession"], []).append(e)
        headers: dict[str, tuple[str | None, list[str]]] = {}
        budget = int(self.params.get("max_header_fetches", 400))
        amb = [acc for acc, es in by_acc.items() if schedule_of(es[0]["form"]) == "13D" and needs_header(es, known)]
        for acc in sorted(amb, reverse=True)[:budget]:  # newest accessions first
            es = by_acc[acc]
            url = f"{ARCHIVES}/data/{int(es[0]['cik'])}/{acc_nodash(acc)}/{acc}-index-headers.html"
            try:
                headers[acc] = parse_header_roles(self.http.get(url, cache_ttl=-1).text)
            except Exception as e:  # noqa: BLE001
                log.debug("13D header %s: %s", acc, e)
        fetched.meta["n_header_fetches"] = len(headers)
        df = build_rows(entries, known, headers, datetime.now(UTC))
        fetched.meta["n_rows"] = df.height
        return {"activist_filings": df}


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    ad = Sec13DGAdapter(settings=ctx.settings, params=dict(ctx.params))
    fetched, rows = ad.run(since=ctx.params.get("since"))
    if fetched.status != "failed" and fetched.watermark:
        ad.save_state(watermark=fetched.watermark)
    return JobResult(status=fetched.status if (fetched.entities_ok or fetched.entities_failed) else "success",
                     rows_written=rows, entities_ok=len(fetched.entities_ok), entities_failed=len(fetched.entities_failed),
                     watermark=fetched.watermark, http_requests=ad.http.n_requests, cache_hits=ad.http.n_cache_hits,
                     message=f"rows={fetched.meta.get('n_rows')} header_fetches={fetched.meta.get('n_header_fetches')}")


