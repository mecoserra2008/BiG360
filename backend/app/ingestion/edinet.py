"""Japan FSA EDINET API v2 -> filings_index + fundamentals_raw (annual / half-year / legacy quarterly reports) and
filings_index for large-shareholding reports.

Source research: docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §1.1 (verified live 2026-10-10 with key).
  GET /api/v2/documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=..   (one call per calendar day; HTTP 200 even
      on auth errors -> check JSON metadata.status / StatusCode)
  GET /api/v2/documents/{docID}?type=5&Subscription-Key=..                 (XBRL->CSV zip: UTF-16LE TSV)
  Code list (no key): https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip (cp932, 1 meta line)
docTypeCode: 120/130 annual (+amendment), 140/150 quarterly (abolished for periods from 2024-04), 160/170 half-year,
             350/360 large-shareholding (+correction).
CSV columns: 要素ID, 項目名, コンテキストID, 相対年度, 連結・個別, 期間・時点, ユニットID, 単位, 値.
Context ids ('CurrentYearDuration', 'Prior1YearInstant_NonConsolidatedMember', 'InterimDuration', ...) are resolved to
dates with the DEI facts (CurrentFiscalYearStartDateDEI, CurrentPeriodEndDateDEI, ...).
Concepts keep the full QName ('jppfs_cor:NetSales', 'jpigp_cor:RevenueIFRS') matching canonical_line_items.yaml 'jp'.
Accounting standard (J-GAAP / IFRS / US-GAAP / JMIS from AccountingStandardsDEI) is recorded in filings_index.items.
PIT: knowledge_date = submitDateTime (JST). Key: EDINET_APIKEY (skips with 'missing key' if absent).
COVERAGE_V3 B1 (2026-11-07): statements of EVERY listed issuer (seed included) are loaded by app.ingestion.edinet_listed
(jobs edinet_listed / edinet_listed_backfill) from the day lists this adapter lands; the incremental run here keeps the
lists + large-shareholding filings (param statements, default true only for mode=backfill). Filers without
consolidated statements (DEI WhetherConsolidatedFinancialStatementsArePrepared=false) are parsed as entity-level.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (add_years, fact_row, period_start_for, filing_row, fiscal_year_for, frame, jst_to_utc,
                                          load_state, parse_num, run_adapter_job, safe_call, save_state, seed_universe,
                                          to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

CODELIST = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
FUND_DOCS = {"120", "130", "140", "150", "160", "170"}
HOLDING_DOCS = {"350", "360"}
FORM_CLASS = {"120": "annual", "130": "annual", "140": "quarterly", "150": "quarterly", "160": "semiannual",
              "170": "semiannual", "350": "holdings", "360": "holdings", "180": "current_report"}
AMENDMENTS = {"130", "150", "170", "360"}
TAXONOMY = {"jppfs_cor": "jppfs", "jpcrp_cor": "jpcrp", "jpigp_cor": "jpigp", "jpdei_cor": "dei", "ifrs-full": "ifrs-full"}
UNIT = {"JPY": "JPY", "shares": "shares", "pure": "pure", "JPYPerShares": "JPY/shares", "USD": "USD",
        "USDPerShares": "USD/shares"}
_CTX = re.compile(r"^(CurrentYear|Prior(\d+)Year|Interim|Prior(\d+)Interim|CurrentYTD|Prior(\d+)YTD|CurrentQuarter|"
                  r"Prior(\d+)Quarter|FilingDate|RecordDate)(Duration|Instant)(?:_(.*))?$")


def _dei(rows: list[list[str]]) -> dict[str, str]:
    return {r[0].split(":", 1)[1]: r[8] for r in rows if r[0].startswith("jpdei_cor:") and len(r) > 8}


def resolve_context(ctx: str, dei: dict[str, str], submit: date) -> tuple[date | None, date | None, str | None, str]:
    """-> (period_start, period_end, fiscal_period, members) or (None, None, ..) when unresolvable."""
    m = _CTX.match(ctx)
    if not m:
        return None, None, None, ""
    kind, ptype, members = m.group(1), m.group(6), m.group(7) or ""
    n = int(next((g for g in m.group(2, 3, 4, 5) if g), "0"))
    fys = to_date(dei.get("CurrentFiscalYearStartDateDEI"))
    fye = to_date(dei.get("CurrentFiscalYearEndDateDEI"))
    cpe = to_date(dei.get("CurrentPeriodEndDateDEI")) or fye
    if kind in ("FilingDate", "RecordDate"):
        return None, submit, None, members
    if fys is None or fye is None or cpe is None:
        return None, None, None, members
    if kind.endswith("Year") or kind == "CurrentYear":
        start, end, fp = add_years(fys, -n), add_years(fye, -n), "FY"
    elif "Interim" in kind:
        start, end, fp = add_years(fys, -n), add_years(cpe, -n), "H1"
    elif "YTD" in kind:
        start, end, fp = add_years(fys, -n), add_years(cpe, -n), "YTD"
    else:  # quarter
        q_end = add_years(cpe, -n)
        start, end, fp = period_start_for(q_end, 3), q_end, None
    if ptype == "Instant":
        return None, end, None, members
    return start, end, fp, members


def parse_edinet_csv(content: bytes, *, source_entity_id: str, filing_id: str, form_type: str, knowledge_date: date,
                     source_id: str = "edinet") -> tuple[list[dict[str, Any]], dict[str, str]]:
    text = content.decode("utf-16")
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    if not rows or rows[0][:3] != ["要素ID", "項目名", "コンテキストID"]:
        raise ValueError(f"EDINET CSV header changed: {rows[0][:3] if rows else None}")
    body = rows[1:]
    dei = _dei(body)
    fye = to_date(dei.get("CurrentFiscalYearEndDateDEI"))
    # B1 (2026-11-07): a filer without consolidated statements (DEI WhetherConsolidatedFinancialStatementsArePrepared
    # = false, common among small caps) reports entity-level figures only. Its cover / summary rows ('その他') were
    # flagged consolidated=True, so the normalizer's "parent-only facts are dropped when the filing has consolidated
    # ones" rule discarded every statement fact of the filing. All facts of such a document are entity-level.
    no_consolidated = str(dei.get("WhetherConsolidatedFinancialStatementsArePreparedDEI", "")).strip().lower() == "false"
    out: list[dict[str, Any]] = []
    for r in body:
        if len(r) < 9 or r[0].startswith("jpdei_cor:"):
            continue
        unit = UNIT.get(r[6])
        if unit is None:
            continue
        val = parse_num(r[8])
        if val is None:
            continue
        start, end, fp, members = resolve_context(r[2], dei, knowledge_date)
        if end is None or end > knowledge_date:
            continue
        prefix = r[0].split(":", 1)[0]
        taxonomy = TAXONOMY.get(prefix, "other")
        concept = r[0].split(":", 1)[1] if taxonomy == "ifrs-full" else r[0]
        consolidated = not (no_consolidated or "NonConsolidatedMember" in members or r[4] == "個別")
        dim_members = re.sub(r"_?NonConsolidatedMember", "", members).strip("_")
        out.append(fact_row(
            source_id=source_id, source_entity_id=source_entity_id, filing_id=filing_id, form_type=form_type,
            taxonomy=taxonomy, concept=concept, label=r[1] or None, period_start=start, period_end=end, value=val,
            unit=unit, knowledge_date=knowledge_date, fiscal_year=fiscal_year_for(end, fye.month if fye else None),
            fiscal_period=fp, dims={"members": dim_members} if dim_members else None, consolidated=consolidated))
    return out, dei


def read_csv_zip(content: bytes) -> list[tuple[str, bytes]]:
    """Main-report CSVs from a type=5 zip (auditor reports 'jpaud' are skipped)."""
    zf = zipfile.ZipFile(io.BytesIO(content))
    return [(n, zf.read(n)) for n in zf.namelist() if n.endswith(".csv") and "/jpaud" not in n and "jpaud-" not in n]


@register_adapter
class EdinetAdapter(SourceAdapter):
    source_id = "edinet"
    regions = ("JP",)
    produces = ("filings_index", "fundamentals_raw")
    requires_keys = ("edinet_apikey",)
    base_url = "https://api.edinet-fsa.go.jp/api/v2"
    rate_limit = RateLimit(1.0, 2)

    def __init__(self, *args: Any, max_docs: int = 400, all_listed: bool = False, until: str | None = None,
                 mode: str = "incremental", backfill_start: str | None = None, chunk_days: int = 60,
                 backfill_years: int = 10, statements: Any = None, **kw: Any):
        super().__init__(*args, **kw)
        self.max_docs = int(max_docs)
        self.all_listed = str(all_listed).lower() in ("1", "true")
        self.until = to_date(until)
        self.mode = str(mode or "incremental").lower()
        self.backfill_start = to_date(backfill_start)
        self.chunk_days = max(1, int(chunk_days))
        self.backfill_years = int(backfill_years)
        # COVERAGE_V3 B1: annual / half-year / quarterly statement CSVs of every listed issuer (the seed included) are
        # loaded by app.ingestion.edinet_listed (job edinet_listed, after this one), which appends per commit instead
        # of upserting: an upsert re-reads the whole edinet fiscal-year partitions (~4 M facts each once all listed
        # issuers are loaded). The incremental run therefore lands the day lists + large-shareholding filings only;
        # the legacy manual backfill (mode=backfill) keeps downloading + upserting unless statements=false.
        self.statements = (self.mode == "backfill") if statements is None else str(statements).lower() in ("1", "true")

    # ------------------------------------------------------------------ discover (no key needed)
    def code_list(self) -> list[dict[str, str]]:
        r = self.http.get(CODELIST, cache_ttl=86400)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        text = zf.read(name).decode("cp932", errors="replace")
        lines = text.splitlines()[1:]
        return list(csv.DictReader(lines))

    def discover(self) -> pl.DataFrame:
        rows = []
        for r in self.code_list():
            sec = (r.get("証券コード") or "").strip()
            rows.append({"source_entity_id": r.get("ＥＤＩＮＥＴコード"), "edinet_code": r.get("ＥＤＩＮＥＴコード"),
                         "name": r.get("提出者名（英字）") or r.get("提出者名"), "name_local": r.get("提出者名"),
                         "country": "JP", "jp_sec_code": sec[:4] if len(sec) == 5 and sec.endswith("0") else (sec or None),
                         "industry_code": r.get("提出者業種"), "industry_scheme": "edinet_industry",
                         "is_active": (r.get("上場区分") or "") == "上場"})
        return self.discover_frame(rows, self.source_id)

    # ------------------------------------------------------------------ fetch
    def _key(self) -> str:
        return self.key("edinet_apikey")

    def day_list(self, d: date) -> list[dict[str, Any]]:
        age = (datetime.now(UTC).date() - d).days
        # backfill: the day list is landed as raw anyway -> no HTTP cache copy (10y of lists would be GBs)
        ttl = 3600 if age <= 7 else (0 if self.mode == "backfill" else 30 * 86400)
        r = self.http.get("documents.json", params={"date": d.isoformat(), "type": 2, "Subscription-Key": self._key()},
                          cache_ttl=ttl)
        j = r.json()
        meta = j.get("metadata") or {}
        if str(meta.get("status", j.get("StatusCode", ""))) != "200":
            raise ValueError(f"EDINET list status {meta.get('status', j.get('StatusCode'))}: {meta.get('message', j.get('message'))}")
        return j.get("results") or []

    def _universe(self, entities: Sequence[str] | None) -> tuple[set[str], set[str]] | None:
        if self.all_listed:
            return None
        if entities:
            ed = {e.upper() for e in entities if e.upper().startswith("E")}
            sec = {(e if len(e) == 5 else e + "0") for e in entities if not e.upper().startswith("E")}
            return ed, sec
        seed = seed_universe(self.settings, countries=["JP"])
        sec = {t + "0" for t in seed.get_column("ticker").to_list()} if seed.height else set()
        return set(), sec

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        state = load_state(self.source_id, self.settings)
        today = datetime.now(UTC).date()
        since_d = to_date(since) or (to_date(state["watermark"]) - timedelta(days=6) if state.get("watermark")
                                     else today - timedelta(days=7))
        end_d = min(self.until or today, today)
        uni = self._universe(entities)
        docs: list[dict[str, Any]] = []
        d = since_d
        while d <= end_d:
            lst = safe_call(lambda d=d: self.day_list(d), res, f"list:{d}", self.log)
            if lst:
                self.save_raw("lists", f"documents_{d.isoformat()}", lst, day=today)
            for x in lst or []:
                code = x.get("docTypeCode")
                if code not in FUND_DOCS | HOLDING_DOCS or x.get("withdrawalStatus") not in (None, "0"):
                    continue
                if code in FUND_DOCS and not self.statements:
                    continue  # B1: loaded (filings_index + facts) by edinet_listed
                target =(x.get("subjectEdinetCode") or x.get("issuerEdinetCode")) if code in HOLDING_DOCS else None
                ed, sec = (x.get("edinetCode") or "", x.get("secCode") or "")
                if uni is not None:
                    in_u = ed in uni[0] or sec in uni[1] or (target is not None and target in uni[0])
                    if code in HOLDING_DOCS and not in_u:
                        continue
                    if code in FUND_DOCS and not in_u:
                        continue
                elif code in FUND_DOCS and not sec:
                    continue
                docs.append(x)
            d += timedelta(days=1)
        landed: list[dict[str, Any]] = []
        n_dl = 0
        for x in docs:
            if x["docTypeCode"] in FUND_DOCS and x.get("csvFlag") == "1" and n_dl < self.max_docs:
                n_dl += 1

                def dl(x=x) -> None:
                    r = self.http.get(f"documents/{x['docID']}", params={"type": 5, "Subscription-Key": self._key()},
                                      cache_ttl=-1)
                    if not r.content.startswith(b"PK"):
                        raise ValueError(f"not a zip: {r.content[:120]!r}")
                    landed.append({**x, "_raw_path": str(self.save_raw("xbrl_csv", x["docID"], r.content, compress=False))})
                safe_call(dl, res, x["docID"], self.log)
            else:
                landed.append(x)
        res.meta["docs"] = landed
        if n_dl >= self.max_docs:
            res.meta["max_docs_hit"] = True
        if not entities and self.until is None and self.mode != "backfill":
            res.watermark = today.isoformat()
            state["watermark"] = res.watermark
            save_state(self.source_id, state, self.settings)
        return res

    # ------------------------------------------------------------------ normalize
    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        fil: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        for x in fetched.meta.get("docs", []):
            code = x["docTypeCode"]
            sub = datetime.strptime(x["submitDateTime"], "%Y-%m-%d %H:%M")
            kd = sub.date()
            ts = jst_to_utc(kd, x["submitDateTime"][11:16])
            entity = x.get("edinetCode") or ""
            if code in HOLDING_DOCS:
                entity = x.get("subjectEdinetCode") or x.get("issuerEdinetCode") or entity
            pe = to_date(x.get("periodEnd"))
            fid = f"{self.source_id}:{x['docID']}"
            items = [f"docTypeCode={code}", f"filer={x.get('edinetCode')}"]
            fp = {"120": "FY", "130": "FY", "160": "H1", "170": "H1"}.get(code)
            fye_month = None
            if x.get("_raw_path"):
                try:
                    for name, content in read_csv_zip(Path(x["_raw_path"]).read_bytes()):
                        if not re.search(r"jp(crp|sps|lvh)\d*", name):
                            continue
                        rows, dei = parse_edinet_csv(content, source_entity_id=entity, filing_id=fid,
                                                     form_type=code, knowledge_date=kd, source_id=self.source_id)
                        facts += rows
                        if dei.get("AccountingStandardsDEI"):
                            items.append(f"accounting_standard={dei['AccountingStandardsDEI']}")
                        if dei.get("TypeOfCurrentPeriodDEI") and dei["TypeOfCurrentPeriodDEI"] in ("Q1", "Q2", "Q3", "Q4", "HY", "FY"):
                            fp = {"HY": "H1"}.get(dei["TypeOfCurrentPeriodDEI"], dei["TypeOfCurrentPeriodDEI"])
                        pe = to_date(dei.get("CurrentPeriodEndDateDEI")) or pe
                        fye = to_date(dei.get("CurrentFiscalYearEndDateDEI"))
                        fye_month = fye.month if fye else fye_month
                except Exception as e:  # noqa: BLE001
                    fetched.entities_failed[f"parse:{x['docID']}"] = f"{type(e).__name__}: {e}"[:300]
            fil.append(filing_row(
                source_id=self.source_id, native_key=x["docID"], source_entity_id=entity, form_type=code,
                form_class=FORM_CLASS.get(code, "other"), knowledge_date=kd, knowledge_ts=ts, period_end=pe,
                fiscal_year=fiscal_year_for(pe, fye_month) if pe and code in FUND_DOCS else None, fiscal_period=fp if code in FUND_DOCS else None,
                is_amendment=code in AMENDMENTS,
                amends_filing_id=f"{self.source_id}:{x['parentDocID']}" if x.get("parentDocID") else None,
                language="ja", url=f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{x['docID']}",
                doc_path=str(Path(x["_raw_path"]).relative_to(self.settings.data_dir)) if x.get("_raw_path") else None,
                has_xbrl=x.get("xbrlFlag") == "1", items=items))
        return {"filings_index": frame(fil, "filings_index"), "fundamentals_raw": frame(facts, "fundamentals_raw")}


def backfill(ctx: Any) -> Any:
    """Historical backfill (docs/COMPANY_COVERAGE_OPTIONS.md F4): walk the daily document list from
    params.backfill_start (default today - backfill_years, EDINET keeps ~10 years) in chunks of chunk_days, land +
    normalize + write each chunk, and persist the resume cursor (state edinet.json 'backfill_cursor') after each chunk.
    Re-running continues at the cursor; params.restart=true starts over. The incremental 'watermark' is untouched."""
    from app.core.schema import conform
    from app.core.storage import write_table
    from app.ingestion.base import MissingApiKey
    from app.scheduler.base import JobResult

    params = dict(getattr(ctx, "params", {}) or {})
    restart = str(params.pop("restart", "false")).lower() in ("1", "true")
    max_chunks = int(params.pop("max_chunks", 10_000))
    for k in ("since", "universe", "tickers", "entities"):
        params.pop(k, None)
    params["mode"] = "backfill"
    params.setdefault("max_docs", 100_000)
    a = EdinetAdapter(settings=ctx.settings, **params)
    try:
        a.check_keys()
    except MissingApiKey as e:
        return JobResult(status="skipped", message=f"missing key: {e}")
    t0 = datetime.now(UTC)
    today = t0.date()
    state = load_state(a.source_id, a.settings)
    start = a.backfill_start or (today - timedelta(days=round(365.25 * a.backfill_years)) + timedelta(days=1))
    cur = start if restart or not state.get("backfill_cursor") else max(start, to_date(state["backfill_cursor"]))
    stop = a.until or today
    rows: dict[str, int] = {}
    n_ok = n_fail = chunks = 0
    while cur <= stop and chunks < max_chunks:
        end = min(cur + timedelta(days=a.chunk_days - 1), stop)
        a.until = end
        fetched = a.fetch(since=cur, entities=None)
        tables = a.normalize(fetched)
        for name, df in tables.items():
            rows[name] = rows.get(name, 0) + write_table(conform(df, name), name, settings=a.settings)
        n_ok += len(fetched.entities_ok)
        n_fail += len(fetched.entities_failed)
        if fetched.meta.get("_circuit_open"):
            return JobResult(status="partial", rows_written=rows, entities_ok=n_ok, entities_failed=n_fail,
                             message=f"circuit open at {cur}..{end}; cursor kept at {cur}")
        state = load_state(a.source_id, a.settings)
        state["backfill_cursor"] = (end + timedelta(days=1)).isoformat()
        state["backfill_start"] = start.isoformat()
        save_state(a.source_id, state, a.settings)
        a.log.info("edinet backfill %s..%s: docs=%d rows=%s", cur, end, len(fetched.meta.get("docs", [])), rows)
        cur = end + timedelta(days=1)
        chunks += 1
    done = cur > stop
    return JobResult(status="partial" if n_fail and not n_ok else "success", rows_written=rows, entities_ok=n_ok,
                     entities_failed=n_fail, http_requests=a.http.n_requests, cache_hits=a.http.n_cache_hits,
                     message=f"backfill {start}..{min(cur, stop)} {'complete' if done else 'in progress (resume)'} "
                             f"({(datetime.now(UTC) - t0).total_seconds():.0f}s)")


def run(ctx: Any) -> Any:
    if str((getattr(ctx, "params", {}) or {}).get("mode", "")).lower() == "backfill":
        return backfill(ctx)
    return run_adapter_job(EdinetAdapter, ctx)
