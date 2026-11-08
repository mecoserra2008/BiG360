"""SEC XBRL company facts -> fundamentals_raw (jobs `sec_xbrl_facts` nightly and `sec_xbrl_bulk` weekly).

Modes (ctx.params.mode):
  * nightly (default): per-CIK `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` for universe CIKs that filed a
    10-K/10-Q/20-F/40-F (+/A), an 8-K with item 2.02 or an XBRL-tagged 6-K since the watermark (from filings_index).
    First run (no watermark) refreshes the whole universe.
  * bulk_companyfacts_zip: streams `Archives/edgar/daily-index/xbrl/companyfacts.zip` (~1.4 GB) to the raw zone and
    parses member JSONs one at a time (never the whole zip in RAM). Default = universe CIKs (upsert); with
    params.all_ciks=true every CIK is parsed, spilled to temporary Parquet, then written per fiscal_year partition with
    overwrite_partition (only sec_edgar partitions are touched).

Kept facts: every version (all filings) of the concepts referenced by contracts/canonical_line_items.yaml (us_gaap,
us_gaap_negate -> us-gaap; ifrs -> ifrs-full; dei:* prefixes) plus dei EntityCommonStockSharesOutstanding,
EntityPublicFloat, EntityNumberOfEmployees. params.all_facts=true keeps every us-gaap / ifrs-full / dei / srt fact.
Concept names are stored WITHOUT namespace prefix; the namespace is in `taxonomy`.
A synthetic dei row `CurrentFiscalYearEndDate` (value MMDD, unit 'MMDD') is added per CIK from the submissions API.

PIT: knowledge_date = companyfacts `filed`. Restatements are separate rows (different filing_id); `is_latest` marks
the most recently filed version of each (entity, concept, period, unit) and `frame` keeps SEC's canonical-period tag.
"""

from __future__ import annotations

import re
import shutil
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import polars as pl
import yaml

from app.core.http import HttpError
from app.core.schema import conform
from app.core.storage import scan_table, write_table
from app.ingestion.base import FetchResult, register_adapter
from app.ingestion.sec_index import (
    ANNUAL,
    ET,
    SEC_DATA,
    SEC_SOURCE,
    XBRL_FORMS,
    SecAdapter,
    base_form,
    cik10,
    make_filing_id,
    parse_since,
    read_existing_filings,
    submissions_recent_by_acc,
)

BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
TAXONOMY_ENUM = {"us-gaap": "us-gaap", "ifrs-full": "ifrs-full", "dei": "dei", "srt": "srt"}
DEI_KEEP = {"EntityCommonStockSharesOutstanding", "EntityPublicFloat", "EntityNumberOfEmployees"}
FP_ENUM = {"FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2", "9M", "YTD"}
_FUNCS = {"nz", "coalesce", "abs", "min", "max"}
_IDENT = re.compile(r"(?:([A-Za-z][A-Za-z\-]*):)?([A-Za-z_][A-Za-z0-9_]*)")

RAW_SCHEMA = {
    "source_id": pl.Utf8, "fiscal_year": pl.Int16, "source_entity_id": pl.Utf8, "issuer_id": pl.Utf8,
    "filing_id": pl.Utf8, "form_type": pl.Utf8, "taxonomy": pl.Utf8, "concept": pl.Utf8, "label": pl.Utf8,
    "period_type": pl.Utf8, "period_start": pl.Date, "period_end": pl.Date, "fiscal_period": pl.Utf8,
    "value": pl.Float64, "unit": pl.Utf8, "decimals": pl.Int16, "dimensions_hash": pl.Utf8, "dimensions_json": pl.Utf8,
    "consolidated": pl.Boolean, "knowledge_date": pl.Date, "knowledge_date_estimated": pl.Boolean,
    "ingested_at": pl.Datetime("us", "UTC"), "frame": pl.Utf8, "is_latest": pl.Boolean,
}


# ----------------------------------------------------------------------------------------------- tag registry


def canonical_tags(contracts_dir: Path) -> dict[str, set[str]]:
    """namespace -> concept names referenced by canonical_line_items.yaml (incl. 'A + B' and nz(...) formulas)."""
    doc = yaml.safe_load((contracts_dir / "canonical_line_items.yaml").read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {"us-gaap": set(), "ifrs-full": set(), "dei": set(DEI_KEEP)}
    keymap = {"us_gaap": "us-gaap", "us_gaap_negate": "us-gaap", "ifrs": "ifrs-full"}
    for item in doc.get("items", []):
        for key, ns in keymap.items():
            for entry in item.get(key) or []:
                for m in _IDENT.finditer(str(entry)):
                    prefix, name = m.group(1), m.group(2)
                    if name in _FUNCS and not prefix:
                        continue
                    target = {"dei": "dei", "us-gaap": "us-gaap", "ifrs-full": "ifrs-full"}.get(prefix or "", ns)
                    out.setdefault(target, set()).add(name)
    return out


# ----------------------------------------------------------------------------------------------- parsing


def companyfacts_to_frame(doc: dict, keep: dict[str, set[str]] | None, now: datetime | None = None,
                          cik: str | None = None) -> pl.DataFrame:
    """One companyfacts JSON -> fundamentals_raw rows (deduplicated on PK, is_latest computed).
    keep=None keeps every fact of us-gaap / ifrs-full / dei / srt. `cik` overrides the document's cik field (some
    bulk members carry "cik": null; the CIK is then taken from the member name CIK##########.json)."""
    now = now or datetime.now(UTC)
    raw_cik = cik if cik is not None else doc.get("cik")
    if raw_cik in (None, "", "None"):
        return pl.DataFrame(schema=RAW_SCHEMA)
    cik = cik10(raw_cik)
    c: dict[str, list] = defaultdict(list)
    for tax, concepts in (doc.get("facts") or {}).items():
        if tax not in TAXONOMY_ENUM:
            continue
        keepset = None if keep is None else keep.get(tax, set())
        for concept, body in (concepts or {}).items():
            if keepset is not None and concept not in keepset:
                continue
            label = body.get("label")
            for unit, facts in (body.get("units") or {}).items():
                for f in facts:
                    end, filed, accn, val = f.get("end"), f.get("filed"), f.get("accn"), f.get("val")
                    if end is None or filed is None or accn is None or val is None:
                        continue
                    c["taxonomy"].append(tax)
                    c["concept"].append(concept)
                    c["label"].append(label)
                    c["unit"].append(unit)
                    c["start"].append(f.get("start"))
                    c["end"].append(end)
                    c["val"].append(float(val))
                    c["accn"].append(accn)
                    fy = f.get("fy")
                    c["fy"].append(fy if isinstance(fy, int) and 1990 <= fy <= 2100 else None)
                    c["fp"].append(f.get("fp"))
                    c["form"].append(f.get("form"))
                    c["filed"].append(filed)
                    c["frame"].append(f.get("frame"))
    if not c:
        return pl.DataFrame(schema=RAW_SCHEMA)
    df = pl.DataFrame(c, schema={
        "taxonomy": pl.Utf8, "concept": pl.Utf8, "label": pl.Utf8, "unit": pl.Utf8, "start": pl.Utf8, "end": pl.Utf8,
        "val": pl.Float64, "accn": pl.Utf8, "fy": pl.Int64, "fp": pl.Utf8, "form": pl.Utf8, "filed": pl.Utf8,
        "frame": pl.Utf8})
    df = df.with_columns(
        pl.col("start").str.to_date(strict=False).alias("period_start"),
        pl.col("end").str.to_date(strict=False).alias("period_end"),
        pl.col("filed").str.to_date(strict=False).alias("knowledge_date"),
    ).filter(pl.col("period_end").is_not_null() & pl.col("knowledge_date").is_not_null())
    out = df.select(
        pl.lit(SEC_SOURCE).alias("source_id"),
        pl.coalesce(pl.col("fy"), pl.col("period_end").dt.year()).cast(pl.Int16).alias("fiscal_year"),
        pl.lit(cik).alias("source_entity_id"),
        pl.lit(None, pl.Utf8).alias("issuer_id"),
        (pl.lit(f"{SEC_SOURCE}:") + pl.col("accn")).alias("filing_id"),
        pl.col("form").alias("form_type"),
        pl.col("taxonomy"),
        pl.col("concept"),
        pl.col("label"),
        pl.when(pl.col("period_start").is_null()).then(pl.lit("instant")).otherwise(pl.lit("duration")).alias("period_type"),
        pl.col("period_start"),
        pl.col("period_end"),
        pl.when(pl.col("fp").is_in(list(FP_ENUM))).then(pl.col("fp")).otherwise(None).alias("fiscal_period"),
        pl.col("val").alias("value"),
        pl.col("unit"),
        pl.lit(None, pl.Int16).alias("decimals"),
        pl.lit("").alias("dimensions_hash"),
        pl.lit(None, pl.Utf8).alias("dimensions_json"),
        pl.lit(True).alias("consolidated"),
        pl.col("knowledge_date"),
        pl.lit(False).alias("knowledge_date_estimated"),
        pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("ingested_at"),
        pl.col("frame"),
    )
    return dedupe_and_mark_latest(out)


PK = ["source_id", "source_entity_id", "filing_id", "concept", "period_start", "period_end", "unit", "dimensions_hash"]
VERSION_KEY = ["source_entity_id", "taxonomy", "concept", "period_start", "period_end", "unit", "dimensions_hash"]


def dedupe_and_mark_latest(df: pl.DataFrame) -> pl.DataFrame:
    """Drop exact PK duplicates (prefer the copy carrying a frame), then flag the latest-filed version per fact."""
    if df.is_empty():
        return df.with_columns(pl.lit(None, pl.Boolean).alias("is_latest")) if "is_latest" not in df.columns else df
    df = (df.sort(["frame", "fiscal_year", "fiscal_period"], nulls_last=True)
            .unique(subset=PK, keep="first", maintain_order=True))
    df = df.sort(["knowledge_date", "filing_id"])
    df = df.with_columns((pl.col("filing_id") == pl.col("filing_id").last().over(VERSION_KEY)).alias("is_latest"))
    return df.sort(["source_entity_id", "taxonomy", "concept", "period_end", "knowledge_date"])


def fiscal_year_end_row(cik: str, subs: dict, now: datetime | None = None) -> dict | None:
    """Synthetic dei:CurrentFiscalYearEndDate fact from the submissions API (fiscalYearEnd 'MMDD')."""
    fye = (subs.get("fiscalYearEnd") or "").strip()
    if not re.fullmatch(r"\d{4}", fye):
        return None
    recent = submissions_recent_by_acc(subs)
    annual = [(a, r) for a, r in recent.items() if base_form(r.get("form", "")) in ANNUAL and r.get("reportDate")]
    if not annual:
        return None
    acc, r = max(annual, key=lambda x: (x[1].get("filingDate") or "", x[0]))
    pe = datetime.strptime(r["reportDate"], "%Y-%m-%d").date()
    return {
        "source_id": SEC_SOURCE, "fiscal_year": pe.year, "source_entity_id": cik10(cik), "issuer_id": None,
        "filing_id": make_filing_id(acc), "form_type": r.get("form"), "taxonomy": "dei",
        "concept": "CurrentFiscalYearEndDate", "label": "Fiscal year end (MMDD, from submissions API)",
        "period_type": "instant", "period_start": None, "period_end": pe, "fiscal_period": "FY",
        "value": float(int(fye)), "unit": "MMDD", "decimals": None, "dimensions_hash": "", "dimensions_json": None,
        "consolidated": True, "knowledge_date": datetime.strptime(r["filingDate"], "%Y-%m-%d").date(),
        "knowledge_date_estimated": False, "ingested_at": now or datetime.now(UTC), "frame": None, "is_latest": True,
    }


# ----------------------------------------------------------------------------------------------- adapter


@register_adapter
class SecXbrlAdapter(SecAdapter):
    source_id = "sec_xbrl"
    produces = ("fundamentals_raw",)

    def __init__(self, *a: Any, params: dict[str, Any] | None = None, **kw: Any):
        super().__init__(*a, **kw)
        self.params = params or {}
        self._keep: dict[str, set[str]] | None = None

    @property
    def keep(self) -> dict[str, set[str]] | None:
        if self.params.get("all_facts"):
            return None
        if self._keep is None:
            self._keep = canonical_tags(self.settings.contracts_dir)
        return self._keep

    # -- which CIKs -----------------------------------------------------------------------------------------------
    def ciks_with_new_filings(self, since: date, universe: set[str] | None) -> tuple[list[str], str | None]:
        fi = read_existing_filings(self.settings, filters=pl.col("knowledge_date") > since)
        if fi.is_empty():
            return [], None
        wm = fi["knowledge_date"].max().isoformat()
        fi = fi.with_columns(pl.col("form_type").map_elements(base_form, return_dtype=pl.Utf8).alias("bf"))
        sel = fi.filter(
            pl.col("bf").is_in(list(XBRL_FORMS))
            | ((pl.col("bf") == "8-K") & pl.col("items").list.contains("2.02").fill_null(False))
            | ((pl.col("bf") == "6-K") & pl.col("has_xbrl"))
        )
        ciks = set(sel["source_entity_id"].to_list())
        if universe is not None:
            ciks &= universe
        return sorted(ciks), wm

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(source_id=SEC_SOURCE, started_at=datetime.now(UTC))
        mode = self.params.get("mode", "nightly")
        res.meta["mode"] = mode
        if mode == "bulk_companyfacts_zip":
            return self._fetch_bulk(res, entities)
        universe = None if self.params.get("all_ciks") else set(self.universe_ciks(self.params))
        s = parse_since(since)
        if entities:
            ciks = sorted({cik10(c) for c in entities})
        elif s is None:
            ciks = sorted(universe or [])
            # full refresh: continue incrementally from what the filings index has already seen
            fi_max = read_existing_filings(self.settings).select(pl.col("knowledge_date").max()).item()
            res.watermark = (fi_max or (datetime.now(ET).date() - timedelta(days=1))).isoformat()
        else:
            ciks, wm = self.ciks_with_new_filings(s, universe)
            res.watermark = wm or s.isoformat()
        for c in ciks:
            try:
                r = self.http.get(f"{SEC_DATA}/api/xbrl/companyfacts/CIK{c}.json", cache_ttl=0)
                res.raw_paths.append(self.save_raw("companyfacts", f"CIK{c}", r.content))
                res.entities_ok.append(c)
            except HttpError as e:
                if e.status == 404:  # no XBRL financial data for this filer
                    res.meta.setdefault("no_xbrl", []).append(c)
                    continue
                res.entities_failed[c] = str(e)[:300]
            except Exception as e:  # noqa: BLE001
                res.entities_failed[c] = str(e)[:300]
        res.meta["ciks"] = ciks
        return res

    def _fetch_bulk(self, res: FetchResult, entities: Sequence[str] | None) -> FetchResult:
        today = datetime.now(ET).date()
        root = self.settings.raw_dir / SEC_SOURCE / "companyfacts_bulk"
        max_age = int(self.params.get("bulk_max_age_days", 6))
        existing = sorted(root.glob("dt=*/companyfacts.zip")) if root.exists() else []
        fresh = [p for p in existing
                 if (today - date.fromisoformat(p.parent.name[3:])).days <= max_age and p.stat().st_size > 10_000_000]
        if fresh:
            zpath = fresh[-1]
        else:
            zpath = self.raw_dir("companyfacts_bulk", today) / "companyfacts.zip"
            self.http.download(BULK_URL, zpath)
        # retention: keep only the newest bulk zip (1.4 GB each)
        for p in existing:
            if p != zpath and p.exists():
                shutil.rmtree(p.parent, ignore_errors=True)
        res.raw_paths.append(zpath)
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist() if n.endswith(".json")]
        if entities:
            want = {f"CIK{cik10(c)}.json" for c in entities}
            names = [n for n in names if n in want]
        elif not self.params.get("all_ciks"):
            want = {f"CIK{c}.json" for c in self.universe_ciks(self.params)}
            names = [n for n in names if n in want]
        res.meta["members"] = names
        res.meta["zip_members_total"] = len(names)
        res.watermark = today.isoformat()
        return res

    # -- normalize ------------------------------------------------------------------------------------------------
    def _fye_rows(self, ciks: Sequence[str], failed: dict[str, str] | None = None) -> list[dict]:
        """Fiscal-year-end rows from the submissions API; a fetch error is recorded in `failed` (F24: counted in
        entities_failed -> the run ends partial instead of silently missing the FYE row)."""
        rows = []
        for c in ciks:
            try:
                s = self.submissions(c, ttl=24 * 3600)
            except Exception as e:  # noqa: BLE001
                if failed is not None:
                    failed[f"submissions:{c}"] = f"{type(e).__name__}: {str(e)[:200]}"
                continue
            if s:
                r = fiscal_year_end_row(c, s)
                if r:
                    rows.append(r)
        return rows

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        now = datetime.now(UTC)
        frames: list[pl.DataFrame] = []
        if fetched.meta.get("mode") == "bulk_companyfacts_zip":
            zpath = fetched.raw_paths[0]
            with zipfile.ZipFile(zpath) as z:
                for name in fetched.meta.get("members", []):
                    try:
                        frames.append(companyfacts_to_frame(orjson.loads(z.read(name)), self.keep, now, cik=name[3:13]))
                        fetched.entities_ok.append(name[3:13])
                    except Exception as e:  # noqa: BLE001
                        fetched.entities_failed[name] = str(e)[:300]
            ciks = [n[3:13] for n in fetched.meta.get("members", [])]
        else:
            for p in fetched.raw_paths:
                try:
                    cik_hint = p.name[3:13] if p.name.startswith("CIK") else None
                    frames.append(companyfacts_to_frame(self.load_raw_json(p), self.keep, now, cik=cik_hint))
                except Exception as e:  # noqa: BLE001
                    fetched.entities_failed[p.name] = str(e)[:300]
            ciks = fetched.entities_ok
        fye = self._fye_rows(ciks, fetched.entities_failed) if not self.params.get("skip_fye") else []
        if fye:
            frames.append(pl.DataFrame(fye, schema=RAW_SCHEMA, orient="row"))
        frames = [f for f in frames if f.height]
        df = pl.concat(frames, how="vertical_relaxed") if frames else pl.DataFrame(schema=RAW_SCHEMA)
        fetched.meta["n_facts"] = df.height
        return {"fundamentals_raw": df}

    def run(self, since=None, entities=None, write: bool = True):  # type: ignore[override]
        """fetch + normalize + explicit per-partition upsert (see upsert_fundamentals_raw)."""
        self.check_keys()
        fetched = self.fetch(since=since, entities=entities)
        df = self.normalize(fetched)["fundamentals_raw"]
        n = upsert_fundamentals_raw(df, self.settings) if write else df.height
        fetched.n_requests, fetched.n_cache_hits = self.http.n_requests, self.http.n_cache_hits
        fetched.finished_at = datetime.now(UTC)
        self.log.info("sec_xbrl run: status=%s facts=%d requests=%d", fetched.status, n, fetched.n_requests)
        return fetched, {"fundamentals_raw": n}

    # -- full-market bulk (all CIKs): spill + per-partition overwrite --------------------------------------------
    def run_bulk_all(self) -> tuple[FetchResult, dict[str, int]]:
        fetched = self.fetch()
        zpath = fetched.raw_paths[0]
        spill = self.settings.data_dir / "tmp" / f"sec_xbrl_bulk_{uuid.uuid4().hex[:8]}"
        spill.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        batch: list[pl.DataFrame] = []
        n_batch = 0
        try:
            with zipfile.ZipFile(zpath) as z:
                for i, name in enumerate(fetched.meta["members"]):
                    try:
                        f = companyfacts_to_frame(orjson.loads(z.read(name)), self.keep, now, cik=name[3:13])
                        if f.height:
                            batch.append(f)
                        fetched.entities_ok.append(name[3:13])
                    except Exception as e:  # noqa: BLE001
                        fetched.entities_failed[name] = str(e)[:300]
                    if sum(b.height for b in batch) > 2_000_000 or (batch and i == len(fetched.meta["members"]) - 1):
                        pl.concat(batch, how="vertical_relaxed").write_parquet(spill / f"b{n_batch:05d}.parquet")
                        n_batch += 1
                        batch = []
            if batch:
                pl.concat(batch, how="vertical_relaxed").write_parquet(spill / f"b{n_batch:05d}.parquet")
            lf = pl.scan_parquet(str(spill / "*.parquet"))
            years = lf.select(pl.col("fiscal_year").unique()).collect()["fiscal_year"].to_list()
            total = 0
            for y in sorted(years):
                part = lf.filter(pl.col("fiscal_year") == y).collect()
                total += write_table(conform(part, "fundamentals_raw"), "fundamentals_raw", mode="overwrite_partition",
                                     settings=self.settings)
            fetched.finished_at = datetime.now(UTC)
            return fetched, {"fundamentals_raw": total}
        finally:
            shutil.rmtree(spill, ignore_errors=True)


def upsert_fundamentals_raw(df: pl.DataFrame, settings) -> int:
    """Upsert into fundamentals_raw (sec_edgar partitions only).

    Workaround: app.core.storage.write_table(mode='upsert') dedupes on the full PK after dropping the partition
    columns, and fundamentals_raw's PK contains the partition column source_id -> ColumnNotFoundError on any existing
    partition. Here each touched fiscal_year partition is merged explicitly (existing + new, new wins on PK) and written
    with overwrite_partition. Equivalent semantics; harmless once the core bug is fixed."""
    if df.is_empty():
        return 0
    df = conform(df, "fundamentals_raw")
    total = 0
    for (fy,), part in df.partition_by(["fiscal_year"], as_dict=True).items():
        existing = _read_sec_partition(int(fy), settings)
        if existing.height:
            merged = pl.concat([existing.select(part.columns), part], how="vertical_relaxed")
            part = merged.unique(subset=PK, keep="last", maintain_order=True)
        write_table(part, "fundamentals_raw", mode="overwrite_partition", settings=settings)
        total += df.filter(pl.col("fiscal_year") == fy).height
    return total


def _read_sec_partition(fy: int, settings, attempts: int = 6) -> pl.DataFrame:
    """The sec_edgar / fiscal_year=fy partition only (not a scan of the whole table, which raced with concurrent
    writers of other sources' partitions: FileNotFoundError on a swapped file); retried while a writer swaps it."""
    import time

    from app.core.storage import partition_dir, table_dir

    d = partition_dir(table_dir("fundamentals_raw", settings), ["source_id", "fiscal_year"], [SEC_SOURCE, fy])
    for i in range(attempts):
        files = [f for f in d.glob("*.parquet")] if d.exists() else []
        if not files:
            return pl.DataFrame()
        try:
            df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
            return df.with_columns(pl.lit(SEC_SOURCE).alias("source_id"), pl.lit(fy).cast(pl.Int16).alias("fiscal_year"))
        except (FileNotFoundError, OSError):
            if i == attempts - 1:
                raise
            time.sleep(2 + 2 * i)
    return pl.DataFrame()


def tag_coverage(df: pl.DataFrame, keep: dict[str, set[str]]) -> dict[str, Any]:
    """Diagnostics: which canonical tags were seen in the given facts (used for the data-issue report)."""
    seen = {(t, c) for t, c in df.select("taxonomy", "concept").unique().iter_rows()}
    out = {}
    for ns, tags in keep.items():
        present = sorted(t for t in tags if (ns, t) in seen)
        out[ns] = {"n_tags": len(tags), "n_seen": len(present), "never_seen": sorted(set(tags) - set(present))}
    return out


# ----------------------------------------------------------------------------------------------- job entry


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    params = dict(ctx.params)
    ad = SecXbrlAdapter(settings=ctx.settings, params=params)
    mode = params.get("mode", "nightly")
    if mode == "bulk_companyfacts_zip" and params.get("all_ciks"):
        fetched, rows = ad.run_bulk_all()
    else:
        since = None if mode == "bulk_companyfacts_zip" else (
            params.get("since") or ctx.watermark or ad.load_state().get("watermark"))
        fetched, rows = ad.run(since=since, entities=params.get("ciks"))
    if fetched.status != "failed" and fetched.watermark and mode != "bulk_companyfacts_zip":
        ad.save_state(watermark=fetched.watermark)
    if mode == "bulk_companyfacts_zip" and fetched.status != "failed" and not params.get("keep_zip") and fetched.raw_paths:
        # coverage v2 W2 storage rule: the 1.4 GB bulk zip is deleted once processed (state: the facts are in
        # fundamentals_raw; the next weekly run downloads a fresh copy, register_backfill falls back to the per-CIK API)
        try:
            Path(fetched.raw_paths[0]).unlink(missing_ok=True)
            shutil.rmtree(Path(fetched.raw_paths[0]).parent, ignore_errors=True)
        except OSError:
            pass
    return JobResult(status=fetched.status, rows_written=rows, entities_ok=len(fetched.entities_ok),
                     entities_failed=len(fetched.entities_failed), watermark=fetched.watermark,
                     http_requests=ad.http.n_requests, cache_hits=ad.http.n_cache_hits,
                     message=f"mode={mode} facts={fetched.meta.get('n_facts')} no_xbrl={len(fetched.meta.get('no_xbrl', []))}")

