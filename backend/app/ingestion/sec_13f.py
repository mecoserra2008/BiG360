"""SEC Form 13F data sets -> institutional_holdings (+ filings_index rows for the 13F filings). Job `sec_13f` (weekly).

Source: quarterly-window zips scraped from the "Form 13F Data Sets" landing page (new windows live under
/files/datastandardsinnovation/, older under /files/structureddata/). Each zip has SUBMISSION, COVERPAGE,
SUMMARYPAGE, OTHERMANAGER(2), SIGNATURE and INFOTABLE (CUSIP, value, shares/principal, put/call, discretion, votes).
The zip (~100 MB) is streamed to data/raw/sec_edgar/form13f/ and read member by member.

Rules
  * Only 13F-HR / 13F-HR/A with REPORTTYPE '13F HOLDINGS REPORT' or 'COMBINATION REPORT' are used.
  * Amendments per (filer, period): the latest non-'NEW HOLDINGS' filing (original or RESTATEMENT) is the base;
    later 'NEW HOLDINGS' amendments add positions. Lines are summed per (filer, period, CUSIP, put/call) (managers
    report the same CUSIP on several lines for different discretion / other-manager splits).
  * VALUE is in dollars for filings since 2023-01-03 and in thousands before; normalized to USD.
  * knowledge_date = FILING_DATE of the filing that supplied the row; filing_id = that accession.
  * security_id resolved via ref-identity's identifier_map (id_type=cusip) when available, else null.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.core.schema import empty_frame
from app.ingestion.base import FetchResult, register_adapter
from app.ingestion.sec_index import (
    SEC_SOURCE,
    SEC_WWW,
    SecAdapter,
    cik10,
    filing_index_url,
    filings_frame,
    make_filing_id,
    try_lookup_many,
)

LANDING = f"{SEC_WWW}/data-research/sec-markets-data/form-13f-data-sets"
VALUE_IN_DOLLARS_FROM = date(2023, 1, 3)
_MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def dataset_links(html: str) -> dict[str, str]:
    """Landing page -> {'01jun2026-31aug2026': url} (newest first order not guaranteed)."""
    out = {}
    for url, key in re.findall(r'href="([^"]*?/((?:\d{2}[a-z]{3}\d{4}-\d{2}[a-z]{3}\d{4})|\d{4}q[1-4])_form13f\.zip)"', html, re.I):
        out[key.lower()] = url if url.startswith("http") else SEC_WWW + url
    return out


def _window_end(key: str) -> date:
    m = re.match(r"\d{2}[a-z]{3}\d{4}-(\d{2})([a-z]{3})(\d{4})", key)
    if m:
        return date(int(m.group(3)), _MON[m.group(2).upper()], int(m.group(1)))
    m = re.match(r"(\d{4})q([1-4])", key)
    return date(int(m.group(1)), int(m.group(2)) * 3, 28) if m else date(1900, 1, 1)


def _read(z: zipfile.ZipFile, name: str, columns: list[str] | None = None) -> pl.DataFrame:
    member = next(n for n in z.namelist() if n.upper().endswith(name))
    df = pl.read_csv(z.read(member), separator="\t", infer_schema=False, quote_char=None, truncate_ragged_lines=True,
                     encoding="utf8-lossy")
    if columns:
        df = df.select([c for c in columns if c in df.columns])
    return df


def _sec_date(col: str) -> pl.Expr:
    return pl.col(col).str.strip_chars().str.to_date("%d-%b-%Y", strict=False)


def parse_13f_zip(path: Path | bytes, now: datetime | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """-> (institutional_holdings rows, filing metadata rows [accession, cik, filer_name, form, filed, period])."""
    now = now or datetime.now(UTC)
    import io  # noqa: PLC0415

    z = zipfile.ZipFile(io.BytesIO(path) if isinstance(path, bytes) else path)
    use, meta = resolve_submissions(z)
    return _holdings_from_infotable(z, use, meta, now)


def resolve_submissions(z: zipfile.ZipFile) -> tuple[pl.DataFrame, pl.DataFrame]:
    """SUBMISSION + COVERPAGE -> (accessions whose INFOTABLE lines are used [ACCESSION_NUMBER, cik, period, filed,
    FILINGMANAGER_NAME, ...], all 13F-HR(/A) holdings-report submissions). Amendment rules: module docstring.
    Shared by parse_13f_zip and the memory-bounded backfill (app.ingestion.sec_13f_backfill)."""
    sub = _read(z, "SUBMISSION.TSV").with_columns(_sec_date("FILING_DATE").alias("filed"),
                                                  _sec_date("PERIODOFREPORT").alias("period"))
    cover = _read(z, "COVERPAGE.TSV")
    keep_cov = [c for c in ("ACCESSION_NUMBER", "ISAMENDMENT", "AMENDMENTTYPE", "FILINGMANAGER_NAME", "REPORTTYPE")
                if c in cover.columns]
    meta = sub.join(cover.select(keep_cov), on="ACCESSION_NUMBER", how="left").filter(
        pl.col("SUBMISSIONTYPE").is_in(["13F-HR", "13F-HR/A"]))
    if "REPORTTYPE" in meta.columns:
        meta = meta.filter(pl.col("REPORTTYPE").is_null()
                           | pl.col("REPORTTYPE").str.to_uppercase().str.contains("HOLDINGS|COMBINATION"))
    meta = meta.with_columns(
        pl.col("CIK").map_elements(cik10, return_dtype=pl.Utf8).alias("cik"),
        pl.col("AMENDMENTTYPE").fill_null("").str.to_uppercase().alias("amd_type") if "AMENDMENTTYPE" in meta.columns
        else pl.lit("").alias("amd_type"),
    ).filter(pl.col("filed").is_not_null() & pl.col("period").is_not_null())
    # amendment resolution per (cik, period)
    meta = meta.sort(["cik", "period", "filed", "ACCESSION_NUMBER"])
    is_add = pl.col("amd_type").str.contains("NEW HOLDINGS") & (pl.col("SUBMISSIONTYPE") == "13F-HR/A")
    meta = meta.with_columns(is_add.alias("is_add"))
    base = (meta.filter(~pl.col("is_add")).group_by(["cik", "period"], maintain_order=True)
                .agg(pl.col("ACCESSION_NUMBER").last().alias("base_acc"), pl.col("filed").last().alias("base_filed")))
    m2 = meta.join(base, on=["cik", "period"], how="left")
    use = m2.filter((pl.col("ACCESSION_NUMBER") == pl.col("base_acc"))
                    | (pl.col("is_add") & (pl.col("base_filed").is_null() | (pl.col("filed") >= pl.col("base_filed")))))
    return use, meta


def _holdings_from_infotable(z: zipfile.ZipFile, use: pl.DataFrame, meta: pl.DataFrame,
                             now: datetime) -> tuple[pl.DataFrame, pl.DataFrame]:
    used_acc = use["ACCESSION_NUMBER"].to_list()
    info = _read(z, "INFOTABLE.TSV", ["ACCESSION_NUMBER", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "FIGI", "VALUE",
                                      "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"])
    info = info.filter(pl.col("ACCESSION_NUMBER").is_in(used_acc)).join(
        use.select("ACCESSION_NUMBER", "cik", "period", "filed", "FILINGMANAGER_NAME"), on="ACCESSION_NUMBER")
    info = info.with_columns(
        pl.col("CUSIP").str.strip_chars().str.to_uppercase().alias("cusip"),
        pl.col("VALUE").str.replace_all(",", "").cast(pl.Float64, strict=False).alias("v"),
        pl.col("SSHPRNAMT").str.replace_all(",", "").cast(pl.Float64, strict=False).alias("sh"),
        pl.col("PUTCALL").fill_null("").str.strip_chars().str.to_lowercase().replace({"": "none"}).alias("put_call"),
    ).with_columns(
        pl.when(pl.col("put_call").is_in(["put", "call"])).then(pl.col("put_call")).otherwise(pl.lit("none")).alias("put_call"),
        pl.when(pl.col("filed") < VALUE_IN_DOLLARS_FROM).then(pl.col("v") * 1000).otherwise(pl.col("v")).alias("v"),
    ).filter(pl.col("cusip").str.len_chars() >= 6)
    hold = info.sort(["filed", "ACCESSION_NUMBER"]).group_by(["cik", "period", "cusip", "put_call"]).agg(
        pl.col("FILINGMANAGER_NAME").last().alias("filer_name"),
        pl.col("sh").sum().alias("shares"),
        pl.col("v").sum().alias("value_usd"),
        pl.col("ACCESSION_NUMBER").last().alias("acc"),
        pl.col("filed").max().alias("knowledge_date"),
        pl.col("NAMEOFISSUER").first().alias("issuer_name"),
        pl.col("TITLEOFCLASS").first().alias("title_of_class"),
        pl.col("FIGI").drop_nulls().first().alias("figi") if "FIGI" in info.columns else pl.lit(None, pl.Utf8).alias("figi"),
        pl.col("SSHPRNAMTTYPE").first().alias("amount_type"),
    )
    out = hold.select(
        pl.col("cik").alias("filer_cik"),
        pl.col("filer_name").fill_null("UNKNOWN"),
        pl.col("period").alias("period_end"),
        pl.col("period").dt.year().cast(pl.Int16).alias("period_end_year"),
        pl.col("cusip"),
        pl.lit(None, pl.Utf8).alias("security_id"),
        pl.col("put_call"),
        pl.col("shares").fill_null(0.0),
        pl.col("value_usd").fill_null(0.0),
        (pl.lit(f"{SEC_SOURCE}:") + pl.col("acc")).alias("filing_id"),
        pl.col("knowledge_date"),
        pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("ingested_at"),
        pl.col("issuer_name").str.strip_chars(),
        pl.col("title_of_class").str.strip_chars(),
        pl.col("figi").str.strip_chars(),
        pl.col("amount_type").str.strip_chars(),
    )
    fil = meta.select(pl.col("ACCESSION_NUMBER").alias("accession"), "cik", pl.col("SUBMISSIONTYPE").alias("form"),
                      "filed", "period", pl.col("FILINGMANAGER_NAME").alias("filer_name"))
    return out, fil


def filings_index_rows(fil: pl.DataFrame, settings: Any, now: datetime) -> pl.DataFrame:
    """13F submission metadata (parse_13f_zip second output) -> filings_index rows, merged with richer existing rows
    written by sec_index (knowledge_ts etc. are not clobbered)."""
    f = fil.unique("accession", keep="last")
    rows = [{
        "filing_id": make_filing_id(r["accession"]), "source_id": SEC_SOURCE, "year": r["filed"].year,
        "source_entity_id": r["cik"], "issuer_id": None, "form_type": r["form"], "form_class": "holdings",
        "period_end": r["period"], "fiscal_year": None, "fiscal_period": None, "knowledge_date": r["filed"],
        "knowledge_ts": None, "is_amendment": r["form"].endswith("/A"), "amends_filing_id": None, "language": "en",
        "url": filing_index_url(r["cik"], r["accession"]), "doc_path": None, "has_xbrl": False, "items": None,
        "ingested_at": now,
    } for r in f.to_dicts()]
    fi = filings_frame(rows)
    from app.ingestion.sec_index import merge_preserving, read_existing_filings  # noqa: PLC0415

    prev = read_existing_filings(settings, fi["filing_id"].to_list())
    if prev.height:
        fi = merge_preserving(fi, prev)
    return fi


@register_adapter
class Sec13FAdapter(SecAdapter):
    source_id = "sec_13f"
    produces = ("institutional_holdings", "filings_index")

    def __init__(self, *a: Any, params: dict[str, Any] | None = None, **kw: Any):
        super().__init__(*a, **kw)
        self.params = params or {}

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(source_id=SEC_SOURCE, started_at=datetime.now(UTC))
        links = dataset_links(self.http.get(LANDING, cache_ttl=6 * 3600).text)
        keys = [k.lower() for k in self.params.get("datasets") or []]
        if not keys:
            ordered = sorted(links, key=_window_end, reverse=True)
            done = set(self.load_state().get("datasets_done") or [])
            n = int(self.params.get("n_latest", 1))
            keys = [k for k in ordered[:n] if k not in done or self.params.get("force")]
        for k in keys:
            if k not in links:
                res.entities_failed[k] = "dataset not on landing page"
                continue
            dest = self.settings.raw_dir / SEC_SOURCE / "form13f" / f"{k}_form13f.zip"
            try:
                if not dest.exists():
                    self.http.download(links[k], dest)
                res.raw_paths.append(dest)
                res.entities_ok.append(k)
            except Exception as e:  # noqa: BLE001
                res.entities_failed[k] = str(e)[:300]
        res.watermark = max((_window_end(k) for k in res.entities_ok), default=None)
        res.watermark = res.watermark.isoformat() if res.watermark else None
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        now = datetime.now(UTC)
        holds, fils = [], []
        for p in fetched.raw_paths:
            h, f = parse_13f_zip(p, now)
            holds.append(h)
            fils.append(f)
        if not holds:
            return {"institutional_holdings": empty_frame("institutional_holdings"), "filings_index": filings_frame([])}
        h = pl.concat(holds, how="vertical_relaxed")
        # several datasets may carry versions of the same key -> latest knowledge wins
        h = h.sort(["knowledge_date", "filing_id"]).unique(["filer_cik", "period_end", "cusip", "put_call"],
                                                           keep="last", maintain_order=True)
        m = try_lookup_many("cusip", h["cusip"].unique().to_list())
        if m:
            sec = {k: v.get("primary_security_id") or v.get("security_id") for k, v in m.items()}
            h = h.with_columns(pl.col("cusip").replace_strict(sec, default=None).alias("security_id"))
        fi = filings_index_rows(pl.concat(fils, how="vertical_relaxed"), self.settings, now)
        fetched.meta["n_holdings"] = h.height
        fetched.meta["n_filers"] = h["filer_cik"].n_unique()
        return {"institutional_holdings": h, "filings_index": fi}


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    ad = Sec13FAdapter(settings=ctx.settings, params=dict(ctx.params))
    fetched, rows = ad.run()
    if fetched.status != "failed" and fetched.entities_ok:
        done = set(ad.load_state().get("datasets_done") or []) | set(fetched.entities_ok)
        ad.save_state(watermark=fetched.watermark, datasets_done=sorted(done))
    return JobResult(status=fetched.status if fetched.entities_ok or fetched.entities_failed else "success",
                     rows_written=rows, entities_ok=len(fetched.entities_ok),
                     entities_failed=len(fetched.entities_failed), watermark=fetched.watermark,
                     http_requests=ad.http.n_requests, cache_hits=ad.http.n_cache_hits,
                     message=f"holdings={fetched.meta.get('n_holdings')} filers={fetched.meta.get('n_filers')}")
