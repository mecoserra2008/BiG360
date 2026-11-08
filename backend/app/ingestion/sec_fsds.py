"""SEC Financial Statement Data Sets (FSDS) -> fundamentals_raw for foreign private issuers (20-F / 40-F / 6-K).

Why: `companyfacts` silently stops at FY2024 for IFRS 20-F filers (the ifrs/2025 taxonomy facts are missing from
companyfacts and frames). The quarterly FSDS zips do contain them (docs/COMPANY_COVERAGE_OPTIONS.md F3, verified
2026-10-11: TSMC ProfitLoss FY2025 = 1,697,604,000,000 TWD, filed 2026-04-16).

Source: https://www.sec.gov/files/dera/data/financial-statement-data-sets/{YYYY}q{N}.zip (2009q1 ->, ~50-60 MB each),
members sub.txt (one row per submission: adsh, cik, name, form, period, fy, fp, fye, filed, prevrpt ...) and num.txt
(adsh, tag, version, [coreg], ddate, qtrs, uom, [segments], value, footnote). TSV, UTF-8.
  * version 'us-gaap/2024' | 'ifrs/2025' | 'dei/2024' | 'srt/2024'; custom tags carry version = adsh (dropped).
  * ddate = period end rounded to month end; qtrs = 0 instant, else duration in quarters (4 = annual).
  * rows with coreg or segments set are dimensional duplicates (co-registrant / axis members) -> dropped.
  * per-share values carry the bare currency as uom (EPS 'TWD'): tags containing 'PerShare' get unit 'TWD/shares'.

Landing: per quarter, the zip is streamed to data/tmp, filtered to submissions of FPI forms (20-F, 40-F, 6-K, +/A) and
of non-US universe CIKs (any form), and the filtered sub/num tables are landed as Parquet under
raw/sec_fsds/extract/{YYYY}q{N}_{sub,num}.parquet; the zip is deleted (params keep_zip=true keeps it).
Normalize: universe non-US CIKs only (params all_fpi=true -> every landed FPI), concepts referenced by
canonical_line_items.yaml (us-gaap / ifrs-full / dei; params all_facts=true -> all standard-taxonomy tags).
Rows: source_id 'sec_fsds', source_entity_id = CIK10 (identity id_type cik), filing_id = 'sec_edgar:<adsh>' (the same
filing as the SEC filings_index row), taxonomy us-gaap / ifrs-full / dei / srt, concept WITHOUT prefix (same as
sec_xbrl). PIT: knowledge_date = sub.filed (real filing date).
Resumable: data/state/sec_fsds.json 'done' = quarters written; a run processes missing quarters oldest-first (each one
landed -> normalized -> written -> marked done), so the weekly job only downloads newly published quarters.
Rate limit: shares the SEC 8 req/s bucket (HttpClient 'sec_edgar', SEC User-Agent).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.core.http import HttpClient, HttpError
from app.core.schema import conform
from app.core.storage import write_table
from app.ingestion._global_common import (fiscal_year_for, load_state, period_start_for, run_adapter_job, save_state)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

FSDS_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{q}.zip"
FPI_FORMS = {"20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A", "20FR12B", "20FR12G", "40FR12B"}
TAXONOMY_OF_VERSION = {"us-gaap": "us-gaap", "ifrs": "ifrs-full", "dei": "dei", "srt": "srt"}
FP_MAP = {"FY": "FY", "Q1": "Q1", "Q2": "Q2", "Q3": "Q3", "Q4": "Q4", "H1": "H1", "H2": "H2", "M9": "9M"}
SUB_COLS = ["adsh", "cik", "name", "countryba", "form", "period", "fye", "fy", "fp", "filed", "prevrpt"]
NUM_COLS = ["adsh", "tag", "version", "ddate", "qtrs", "uom", "value"]


def quarters_between(start: str, end: str) -> list[str]:
    """'2009q1', '2010q2' -> ['2009q1', ..., '2010q2']."""
    y0, q0 = int(start[:4]), int(start[-1])
    y1, q1 = int(end[:4]), int(end[-1])
    out = []
    while (y0, q0) <= (y1, q1):
        out.append(f"{y0}q{q0}")
        q0 += 1
        if q0 == 5:
            y0, q0 = y0 + 1, 1
    return out


def last_published_quarter(today: date) -> str:
    """The quarter before the current one (FSDS for quarter N is published a few days after it ends)."""
    q = (today.month - 1) // 3 + 1
    y, q = (today.year, q - 1) if q > 1 else (today.year - 1, 4)
    return f"{y}q{q}"


def _read_tsv(zf: zipfile.ZipFile, name: str, cols: list[str]) -> pl.DataFrame:
    raw = zf.read(name)
    head = raw[: raw.find(b"\n")].decode("utf-8", "replace").rstrip("\r").split("\t")
    use = [c for c in cols + ["coreg", "segments"] if c in head]
    return pl.read_csv(io.BytesIO(raw), separator="\t", quote_char=None, columns=use, infer_schema_length=0,
                       encoding="utf8-lossy", truncate_ragged_lines=True)


def extract_quarter(zip_bytes_or_path: bytes | Path, ciks: set[str] | None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Filter an FSDS quarter zip -> (sub, num) for FPI-form submissions + submissions of `ciks` (int-string CIKs).
    num keeps standard-taxonomy, non-dimensional, non-co-registrant rows only."""
    src = io.BytesIO(zip_bytes_or_path) if isinstance(zip_bytes_or_path, bytes) else zip_bytes_or_path
    with zipfile.ZipFile(src) as zf:
        sub = _read_tsv(zf, "sub.txt", SUB_COLS)
        keep = pl.col("form").is_in(list(FPI_FORMS))
        if ciks:
            keep = keep | pl.col("cik").is_in(list(ciks))
        sub = sub.filter(keep)
        num = _read_tsv(zf, "num.txt", NUM_COLS)
    num = num.filter(pl.col("adsh").is_in(sub["adsh"].implode()) & pl.col("version").str.contains("/"))
    for c in ("coreg", "segments"):
        if c in num.columns:
            num = num.filter(pl.col(c).is_null() | (pl.col(c) == "")).drop(c)
    num = num.filter(pl.col("value").is_not_null() & (pl.col("value") != ""))
    for c in SUB_COLS:
        if c not in sub.columns:
            sub = sub.with_columns(pl.lit(None, pl.Utf8).alias(c))
    return sub.select(SUB_COLS), num.select(NUM_COLS)


def fsds_to_facts(sub: pl.DataFrame, num: pl.DataFrame, *, ciks: set[str] | None,
                  keep: dict[str, set[str]] | None, source_id: str = "sec_fsds") -> pl.DataFrame:
    """Landed sub/num -> fundamentals_raw frame (vectorized)."""
    if ciks is not None:
        sub = sub.filter(pl.col("cik").is_in(list(ciks)))
    if sub.is_empty() or num.is_empty():
        from app.ingestion._global_common import frame

        return frame([], "fundamentals_raw")
    df = num.join(sub, on="adsh", how="inner")
    df = df.with_columns(pl.col("version").str.split("/").list.first().replace_strict(TAXONOMY_OF_VERSION, default=None)
                         .alias("taxonomy")).filter(pl.col("taxonomy").is_not_null())
    if keep is not None:
        ok = pl.lit(False)
        for ns, names in keep.items():
            ok = ok | ((pl.col("taxonomy") == ns) & pl.col("tag").is_in(sorted(names)))
        df = df.filter(ok)
    df = df.with_columns(
        pl.col("ddate").str.strptime(pl.Date, "%Y%m%d", strict=False).alias("period_end"),
        pl.col("filed").str.strptime(pl.Date, "%Y%m%d", strict=False).alias("knowledge_date"),
        pl.col("qtrs").cast(pl.Int32, strict=False).alias("__q"),
        pl.col("value").cast(pl.Float64, strict=False).alias("value"),
        pl.col("fye").str.slice(0, 2).cast(pl.Int32, strict=False).alias("__fyem"),
    ).filter(pl.col("period_end").is_not_null() & pl.col("knowledge_date").is_not_null() & pl.col("value").is_not_null()
             & pl.col("__q").is_not_null())
    if df.is_empty():
        from app.ingestion._global_common import frame

        return frame([], "fundamentals_raw")
    # period_start / fiscal_year per distinct (end, qtrs, fye) - few thousand combos
    combos = df.select("period_end", "__q", "__fyem").unique()
    starts, fys = [], []
    for end, q, fm in combos.iter_rows():
        starts.append(None if q == 0 else period_start_for(end, 3 * q))
        fys.append(fiscal_year_for(end, fm))
    combos = combos.with_columns(pl.Series("period_start", starts, dtype=pl.Date), pl.Series("fiscal_year", fys, dtype=pl.Int16))
    df = df.join(combos, on=["period_end", "__q", "__fyem"], how="left", nulls_equal=True)
    unit = (pl.when(pl.col("uom").str.contains(r"^[A-Za-z]{3}/shares$", literal=False))
            .then(pl.col("uom").str.slice(0, 3).str.to_uppercase() + pl.lit("/shares"))
            # FSDS stores per-share values with the bare currency (EPS uom 'TWD') -> 'TWD/shares' like companyfacts
            .when(pl.col("uom").str.contains(r"^[A-Za-z]{3}$") & pl.col("tag").str.contains("PerShare"))
            .then(pl.col("uom").str.to_uppercase() + pl.lit("/shares"))
            .when(pl.col("uom").str.contains(r"^[A-Za-z]{3}$")).then(pl.col("uom").str.to_uppercase())
            .otherwise(pl.col("uom")))
    now = datetime.now(UTC)
    out = df.select(
        pl.lit(source_id).alias("source_id"), "fiscal_year",
        pl.col("cik").str.zfill(10).alias("source_entity_id"), pl.lit(None, pl.Utf8).alias("issuer_id"),
        (pl.lit("sec_edgar:") + pl.col("adsh")).alias("filing_id"), pl.col("form").alias("form_type"), "taxonomy",
        pl.col("tag").alias("concept"), pl.lit(None, pl.Utf8).alias("label"),
        pl.when(pl.col("__q") == 0).then(pl.lit("instant")).otherwise(pl.lit("duration")).alias("period_type"),
        "period_start", "period_end", pl.col("fp").replace_strict(FP_MAP, default=None).alias("fiscal_period"),
        "value", unit.alias("unit"), pl.lit(None, pl.Int16).alias("decimals"), pl.lit("").alias("dimensions_hash"),
        pl.lit(None, pl.Utf8).alias("dimensions_json"), pl.lit(True).alias("consolidated"), "knowledge_date",
        pl.lit(False).alias("knowledge_date_estimated"), pl.lit(now).alias("ingested_at"),
    )
    pk = ["source_id", "source_entity_id", "filing_id", "concept", "period_start", "period_end", "unit", "dimensions_hash"]
    out = out.unique(subset=pk, keep="first", maintain_order=True)
    # latest-filed version flag within this batch (per entity/concept/period/unit)
    vk = ["source_entity_id", "concept", "period_start", "period_end", "unit"]
    out = out.sort(["knowledge_date", "filing_id"]).with_columns(
        (pl.col("filing_id") == pl.col("filing_id").last().over(vk, mapping_strategy="group_to_rows")).alias("is_latest"))
    return conform(out, "fundamentals_raw")


@register_adapter
class SecFsdsAdapter(SourceAdapter):
    source_id = "sec_fsds"
    regions = ("EU", "UK", "JP", "KR", "CN", "HK", "LATAM", "APAC", "CA", "OTHER")
    produces = ("fundamentals_raw",)
    cache_ttl = 0

    def __init__(self, *args: Any, start: str = "2009q1", end: str | None = None, quarters: str | None = None,
                 all_fpi: bool = False, all_facts: bool = False, keep_zip: bool = False, ciks: str | None = None,
                 redo: bool = False, redownload: bool = False, **kw: Any):
        from app.core.config import get_settings

        settings = kw.get("settings") or (args[0] if args else None) or get_settings()
        http = kw.get("http") or HttpClient("sec_edgar", settings=settings)
        super().__init__(settings=settings, http=http)  # unknown job params (universe, tickers ...) are ignored
        self.start, self.end = str(start), (str(end) if end else None)
        self.quarters = [q.strip() for q in str(quarters).split(",") if q.strip()] if quarters else None
        self.all_fpi = str(all_fpi).lower() in ("1", "true")
        self.all_facts = str(all_facts).lower() in ("1", "true")
        self.keep_zip = str(keep_zip).lower() in ("1", "true")
        self.extra_ciks = {str(int(c)) for c in str(ciks).split(",") if c.strip()} if ciks else set()
        self.redo = str(redo).lower() in ("1", "true")  # re-normalize done quarters (from landed extracts)
        self.redownload = str(redownload).lower() in ("1", "true")  # also re-download the zips

    # ------------------------------------------------------------------ universe
    def universe_ciks(self) -> set[str]:
        """Int-string CIKs of non-US issuers (or SEC foreign filer categories) in identifier_map / issuers."""
        from app.core.storage import read_table

        out = set(self.extra_ciks)
        try:
            iss = read_table("issuers", columns=["issuer_id", "region", "cik", "filer_category"])
            im = read_table("identifier_map", columns=["id_type", "id_value", "issuer_id", "confidence"])
        except Exception:  # noqa: BLE001
            return out
        foreign = iss.filter((pl.col("region") != "US") | pl.col("filer_category").is_in(["foreign_private_20f", "foreign_40f"]))
        ids = set(foreign["issuer_id"].to_list())
        out |= {str(int(c)) for c in foreign["cik"].drop_nulls().to_list() if str(c).strip().isdigit()}
        cm = im.filter((pl.col("id_type") == "cik") & pl.col("issuer_id").is_in(list(ids)) & (pl.col("confidence") >= 0.9))
        out |= {str(int(c)) for c in cm["id_value"].to_list() if str(c).strip().isdigit()}
        return out

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([{"source_entity_id": c.zfill(10), "cik": c.zfill(10), "name": c}
                                    for c in sorted(self.universe_ciks())], self.source_id)

    def keep(self) -> dict[str, set[str]] | None:
        if self.all_facts:
            return None
        from app.ingestion.sec_xbrl import canonical_tags

        k = canonical_tags(self.settings.contracts_dir)
        k.setdefault("srt", set())
        return k

    # ------------------------------------------------------------------ fetch
    def extract_paths(self, q: str) -> tuple[Path, Path]:
        d = self.settings.raw_dir / self.source_id / "extract"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{q}_sub.parquet", d / f"{q}_num.parquet"

    def pending_quarters(self) -> list[str]:
        if self.quarters:
            return self.quarters
        qs = quarters_between(self.start, self.end or last_published_quarter(datetime.now(UTC).date()))
        done = set(load_state(self.source_id, self.settings).get("done", {}))
        return qs if self.redo else [q for q in qs if q not in done]

    def land_quarter(self, q: str, ciks: set[str]) -> tuple[Path, Path] | None:
        sp, np_ = self.extract_paths(q)
        if sp.exists() and np_.exists() and not self.redownload:
            return sp, np_
        tmp = self.settings.data_dir / "tmp" / "sec_fsds" / f"{q}.zip"
        try:
            self.http.download(FSDS_URL.format(q=q), tmp)
        except HttpError as e:
            if e.status in (403, 404):
                return None  # not published (yet)
            raise
        sub, num = extract_quarter(tmp, ciks)
        sub.write_parquet(sp)
        num.write_parquet(np_)
        if self.keep_zip:
            dest = self.settings.raw_dir / self.source_id / "zips" / f"{q}.zip"
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp.replace(dest)
        else:
            tmp.unlink(missing_ok=True)
        return sp, np_

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        ciks = self.universe_ciks() | {str(int(e)) for e in (entities or []) if str(e).strip().isdigit()}
        for q in self.pending_quarters():
            try:
                paths = self.land_quarter(q, ciks)
            except Exception as e:  # noqa: BLE001
                res.entities_failed[q] = f"{type(e).__name__}: {e}"[:300]
                continue
            if paths is None:
                res.meta.setdefault("unpublished", []).append(q)
                continue
            res.raw_paths += list(paths)
            res.entities_ok.append(q)
        res.meta["ciks"] = sorted(ciks)
        if entities:
            res.meta["only_ciks"] = sorted({str(int(e)) for e in entities if str(e).strip().isdigit()})
        return res

    # ------------------------------------------------------------------ normalize
    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        ciks = None if self.all_fpi else set(fetched.meta.get("only_ciks") or fetched.meta.get("ciks") or [])
        keep = self.keep()
        parts = []
        subs = sorted(p for p in fetched.raw_paths if p.name.endswith("_sub.parquet"))
        for sp in subs:
            np_ = sp.with_name(sp.name.replace("_sub.parquet", "_num.parquet"))
            parts.append(fsds_to_facts(pl.read_parquet(sp), pl.read_parquet(np_), ciks=ciks, keep=keep,
                                       source_id=self.source_id))
        if not parts:
            from app.ingestion._global_common import frame

            return {"fundamentals_raw": frame([], "fundamentals_raw")}
        return {"fundamentals_raw": pl.concat(parts, how="vertical_relaxed")}

    # ------------------------------------------------------------------ run: one quarter at a time (resumable)
    def run(self, since=None, entities=None, write: bool = True):  # type: ignore[override]
        self.check_keys()
        total = FetchResult(self.source_id, started_at=datetime.now(UTC))
        rows = {"fundamentals_raw": 0}
        qs = self.pending_quarters()
        st0 = load_state(self.source_id, self.settings)
        # universe CIKs added since earlier quarters were written -> re-normalize those quarters from the extracts
        if write and not entities and not self.all_fpi and not self.quarters and st0.get("ciks_done") is not None:
            new = sorted(self.universe_ciks() - set(st0["ciks_done"]))
            if new:
                for q in sorted(st0.get("done", {})):
                    sp, np_ = self.extract_paths(q)
                    if sp.exists() and np_.exists():
                        df = fsds_to_facts(pl.read_parquet(sp), pl.read_parquet(np_), ciks=set(new), keep=self.keep(),
                                           source_id=self.source_id)
                        if df.height:
                            rows["fundamentals_raw"] += write_table(df, "fundamentals_raw", settings=self.settings)
                self.log.info("sec_fsds: re-normalized %d done quarters for %d new CIKs", len(st0.get("done", {})), len(new))
        for q in qs:
            sub = SecFsdsAdapter.__new__(SecFsdsAdapter)
            sub.__dict__.update(self.__dict__)
            sub.quarters = [q]
            fetched = sub.fetch(since=since, entities=entities)
            total.entities_failed.update(fetched.entities_failed)
            total.meta.setdefault("unpublished", []).extend(fetched.meta.get("unpublished", []))
            if not fetched.raw_paths:
                continue
            df = sub.normalize(fetched)["fundamentals_raw"]
            n = write_table(df, "fundamentals_raw", settings=self.settings) if write and df.height else df.height
            rows["fundamentals_raw"] += n
            total.entities_ok.append(q)
            if write and not entities and not self.all_fpi:
                st = load_state(self.source_id, self.settings)
                st.setdefault("done", {})[q] = {"rows": n, "at": datetime.now(UTC).isoformat(timespec="seconds"),
                                                 "n_ciks": len(fetched.meta.get("ciks", []))}
                save_state(self.source_id, st, self.settings)
            self.log.info("sec_fsds %s: %d rows", q, n)
        if write and not entities and not self.all_fpi and not self.quarters:
            st = load_state(self.source_id, self.settings)
            st["ciks_done"] = sorted(self.universe_ciks())
            save_state(self.source_id, st, self.settings)
        total.n_requests, total.n_cache_hits = self.http.n_requests, self.http.n_cache_hits
        total.watermark = max(total.entities_ok) if total.entities_ok else None
        total.finished_at = datetime.now(UTC)
        return total, rows


def run(ctx: Any) -> Any:
    return run_adapter_job(SecFsdsAdapter, ctx)
