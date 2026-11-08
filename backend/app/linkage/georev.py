"""A5 company -> country: geographic segment revenue (job `linkage_georev`) -> company_geo_revenue.

Why FSDS: SEC companyfacts / frames drop dimensional facts. The SEC Financial Statement Data Sets (quarterly zips,
2009q1 ->, ~50-100 MB) keep them in num.txt `segments` ("Geographical=US;", "MajorCustomers=WalmartInc;",
multi-axis rows joined by ';'); SEC already turns srt country members into ISO2 ("Geographical=JP;"), custom members
keep their element name minus the "Member" suffix ("Geographical=SoutheastAsia;"). Verified 2026-10-12 (2025q2 and
2015q1 both carry the column: the whole history was re-published with segments).

Landing (resumable, detached-safe): per quarter the zip is streamed to data/tmp/linkage/fsds, num.txt is extracted
and scanned lazily (polars streaming, ~3 s), and only these rows are kept:
  * standard-taxonomy, non co-registrant facts whose segments contain Geographical= or MajorCustomers=
    (tags filtered by KEEP_TAG_RX: revenue, concentration %, long-lived assets / PP&E, receivables)
  * the non-dimensional revenue totals of the same submissions (denominators for shares)
-> raw/linkage/fsds/{q}_num.parquet + {q}_sub.parquet; zip + txt deleted; state data/state/linkage_fsds.json.

Normalize (vectorized, all landed quarters): single-axis Geographical rows of the preferred revenue concept per
(filing, period) -> one row per member with member_type country / region / domestic / rest_of_world / other,
ISO2 (country members, US / CN / CA sub-national regions -> their country), region code (app.linkage.members) and
share = value / reported total (else / sum of geographic members). PIT: knowledge_date = sub.filed.
IFRS dimensional facts already in fundamentals_raw (ESEF / CMF / BIVA ... GeographicalAreasAxis) are added with the
same member normalization (source_id of the original row).
"""

from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from app.core.config import Settings
from app.linkage import members as M
from app.linkage.common import (load_state, log, now_utc, raw_dir, save_state, settings_of, tmp_dir, update_progress,
                                write)

FSDS_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{q}.zip"
SUB_COLS = ["adsh", "cik", "name", "sic", "countryba", "countryinc", "form", "period", "fy", "fp", "fye", "filed"]
NUM_COLS = ["adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value"]
KEEP_TAG_RX = (r"(?i)^(Revenue|Revenues|SalesRevenue|RevenueFromContract|RevenuesNet|RevenueNet|"
               r"ConcentrationRiskPercentage|NoncurrentAssets|LongLivedAssets|PropertyPlantAndEquipmentNet|"
               r"AccountsReceivableNetCurrent|TradeAndOtherCurrentReceivables|OperatingIncomeLoss$|Assets$)")
#: revenue concepts in order of preference (per filing / period the most-populated one wins, ties by this order)
REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "Revenue",
                "RevenueFromContractsWithCustomers", "SalesRevenueGoodsNet", "RevenuesNetOfInterestExpense",
                "SalesRevenueServicesNet", "RevenueFromContractWithCustomerProductAndServiceExcludingAssessedTax"]
ASSET_TAGS = ["LongLivedAssets", "NoncurrentAssets", "PropertyPlantAndEquipmentNet",
              "NoncurrentAssetsOtherThanFinancialInstrumentsDeferredTaxAssetsPostemploymentBenefitAssetsAndRightsArisingUnderInsuranceContracts"]
STATE = "fsds"


# ================================================================================================ quarters
def quarters_between(start: str, end: str) -> list[str]:
    y0, q0, y1, q1 = int(start[:4]), int(start[-1]), int(end[:4]), int(end[-1])
    out = []
    while (y0, q0) <= (y1, q1):
        out.append(f"{y0}q{q0}")
        y0, q0 = (y0 + 1, 1) if q0 == 4 else (y0, q0 + 1)
    return out


def last_published_quarter(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    y, q = (d.year, q - 1) if q > 1 else (d.year - 1, 4)
    return f"{y}q{q}"


# ================================================================================================ landing
def extract_zip(zpath, work) -> tuple[pl.DataFrame, pl.DataFrame]:
    """FSDS zip -> (sub, num) filtered to geographic / major-customer facts + revenue totals of those filings."""
    with zipfile.ZipFile(zpath) as zf:
        zf.extract("num.txt", work)
        sub = pl.read_csv(zf.read("sub.txt"), separator="\t", quote_char=None, infer_schema=False,
                          encoding="utf8-lossy", truncate_ragged_lines=True)
    num_path = work / "num.txt"
    try:
        lf = pl.scan_csv(num_path, separator="\t", quote_char=None, infer_schema=False, encoding="utf8-lossy",
                         truncate_ragged_lines=True)
        cols = lf.collect_schema().names()
        lf = lf.select([c for c in NUM_COLS if c in cols])
        if "segments" not in cols:
            raise ValueError("FSDS num.txt without a segments column")
        base = lf.filter(pl.col("version").str.contains("/", literal=True)
                         & (pl.col("coreg").is_null() | (pl.col("coreg") == ""))
                         & pl.col("tag").str.contains(KEEP_TAG_RX))
        dims = base.filter(pl.col("segments").str.contains("Geographical=|MajorCustomers=")).collect(engine="streaming")
        adsh = dims.get_column("adsh").unique().implode()
        tot = base.filter((pl.col("segments").is_null() | (pl.col("segments") == ""))
                          & pl.col("tag").is_in(REVENUE_TAGS + ASSET_TAGS) & pl.col("adsh").is_in(adsh)
                          ).collect(engine="streaming")
    finally:
        num_path.unlink(missing_ok=True)
    num = pl.concat([dims, tot], how="vertical_relaxed").drop("coreg", strict=False)
    sub = sub.filter(pl.col("adsh").is_in(num.get_column("adsh").unique().implode()))
    for c in SUB_COLS:
        if c not in sub.columns:
            sub = sub.with_columns(pl.lit(None, pl.Utf8).alias(c))
    return sub.select(SUB_COLS), num


def land(settings: Settings | None = None, start: str = "2009q1", end: str | None = None, max_quarters: int = 999,
         newest_first: bool = True, redo: bool = False) -> dict[str, Any]:
    from app.core.http import HttpError  # noqa: PLC0415
    from app.linkage.common import http  # noqa: PLC0415

    s = settings_of(settings)
    out_d, work = raw_dir("fsds", s), tmp_dir("fsds", s)
    st = load_state(STATE, s)
    done = st.setdefault("done", {})
    qs = quarters_between(start, end or last_published_quarter(datetime.now(UTC).date()))
    todo = [q for q in qs if redo or q not in done]
    if newest_first:
        todo = todo[::-1]
    cl = http("sec_edgar", s)
    rep: dict[str, Any] = {"landed": [], "unpublished": [], "failed": {}}
    for q in todo[:max_quarters]:
        z = work / f"{q}.zip"
        try:
            if not z.exists():
                cl.download(FSDS_URL.format(q=q), z)
            sub, num = extract_zip(z, work)
        except HttpError as e:
            if e.status in (403, 404):
                rep["unpublished"].append(q)
                continue
            rep["failed"][q] = str(e)[:200]
            continue
        except Exception as e:  # noqa: BLE001 - corrupt zip etc.: drop it, retry next run
            rep["failed"][q] = f"{type(e).__name__}: {e}"[:200]
            z.unlink(missing_ok=True)
            continue
        sub.write_parquet(out_d / f"{q}_sub.parquet")
        num.write_parquet(out_d / f"{q}_num.parquet")
        z.unlink(missing_ok=True)
        st = load_state(STATE, s)
        st.setdefault("done", {})[q] = {"num": num.height, "sub": sub.height, "at": now_utc().isoformat(timespec="seconds")}
        save_state(STATE, st, s)
        rep["landed"].append(q)
        log.info("linkage fsds %s: %d num rows, %d submissions", q, num.height, sub.height)
    cl.close()
    return rep


def landed(settings: Settings | None = None) -> tuple[pl.LazyFrame, pl.LazyFrame] | None:
    d = raw_dir("fsds", settings)
    nums, subs = sorted(d.glob("*_num.parquet")), sorted(d.glob("*_sub.parquet"))
    if not nums:
        return None
    num = pl.concat([pl.scan_parquet(p).with_columns(pl.lit(p.name[:6]).alias("fsds_q")) for p in nums],
                    how="diagonal_relaxed")
    sub = pl.concat([pl.scan_parquet(p) for p in subs], how="diagonal_relaxed").unique("adsh")
    return num, sub


# ================================================================================================ normalize
def normalize_fsds(num: pl.DataFrame, sub: pl.DataFrame, settings: Settings | None = None) -> pl.DataFrame:
    """Landed FSDS rows -> company_geo_revenue frame (revenue + long-lived assets by geography)."""
    num = num.with_columns(pl.col("segments").fill_null(""))
    geo = num.filter(pl.col("segments").str.contains(r"^Geographical=[^;]*;$"))
    geo = geo.with_columns(pl.col("segments").str.extract(r"^Geographical=([^;]*);$").alias("member_raw"),
                           pl.when(pl.col("tag").is_in(REVENUE_TAGS)).then(pl.lit("revenue"))
                           .when(pl.col("tag").is_in(ASSET_TAGS)).then(pl.lit("long_lived_assets"))
                           .otherwise(None).alias("measure")).filter(pl.col("measure").is_not_null())
    if geo.is_empty():
        return pl.DataFrame()
    key = ["adsh", "ddate", "qtrs", "uom", "measure"]
    # preferred concept per (filing, period, measure): most members, ties by REVENUE_TAGS / ASSET_TAGS order
    rank = {t: i for i, t in enumerate(REVENUE_TAGS + ASSET_TAGS)}
    best = (geo.group_by(key + ["tag"]).agg(pl.len().alias("n"))
            .with_columns(pl.col("tag").replace_strict(rank, default=99, return_dtype=pl.Int32).alias("r"))
            .sort(["n", "r"], descending=[True, False]).unique(key, keep="first").select(key + ["tag"]))
    geo = geo.join(best, on=key + ["tag"], how="inner")
    totals = (num.filter(pl.col("segments") == "").select("adsh", "tag", "ddate", "qtrs", "uom",
                                                         pl.col("value").cast(pl.Float64, strict=False).alias("total"))
              .unique(["adsh", "tag", "ddate", "qtrs", "uom"]))
    geo = geo.join(totals, on=["adsh", "tag", "ddate", "qtrs", "uom"], how="left")
    geo = geo.join(sub.select("adsh", "cik", "name", "countryba", "countryinc", "form", "fp", "filed"), on="adsh",
                   how="left")
    geo = geo.with_columns(pl.col("value").cast(pl.Float64, strict=False),
                           pl.col("ddate").str.strptime(pl.Date, "%Y%m%d", strict=False).alias("period_end"),
                           pl.col("filed").str.strptime(pl.Date, "%Y%m%d", strict=False).alias("knowledge_date"),
                           pl.col("qtrs").cast(pl.Int16, strict=False).alias("qtrs"),
                           pl.coalesce(pl.col("countryba").replace("", None), pl.col("countryinc").replace("", None)
                                       ).alias("company_country"))
    geo = geo.filter(pl.col("value").is_not_null() & pl.col("period_end").is_not_null()
                     & pl.col("knowledge_date").is_not_null())
    return finish(geo.select(
        pl.lit("sec_fsds").alias("source_id"), pl.col("cik").str.zfill(10).alias("entity_id"), "name",
        (pl.lit("sec_edgar:") + pl.col("adsh")).alias("filing_id"), pl.col("form").alias("form_type"), "measure",
        pl.col("tag").alias("concept"), "member_raw", "period_end", "qtrs", "value",
        pl.col("uom").str.to_uppercase().alias("currency"), "total", "knowledge_date", "company_country",
        pl.col("fp").alias("fiscal_period")), settings)


def finish(df: pl.DataFrame, settings: Settings | None = None, extra_ids: pl.DataFrame | None = None) -> pl.DataFrame:
    """Member normalization, shares, issuer ids -> contract columns."""
    if df.is_empty():
        return df
    mm = M.map_members(df.select("member_raw", "company_country").unique(), settings)
    df = df.join(mm, on=["member_raw", "company_country"], how="left", nulls_equal=True)
    gk = ["source_id", "entity_id", "filing_id", "measure", "concept", "period_end", "qtrs", "currency"]
    df = df.with_columns(
        pl.col("value").filter(pl.col("member_type") != "other").sum().over(gk).alias("__sum"),
        pl.col("member_type").filter(pl.col("member_type") == "country").len().over(gk).alias("n_countries"))
    df = df.with_columns(
        pl.when(pl.col("total").is_not_null() & (pl.col("total") > 0)).then(pl.col("total"))
        .otherwise(pl.col("__sum")).alias("denominator"))
    df = df.with_columns(
        pl.when((pl.col("denominator") > 0) & (pl.col("member_type") != "other"))
        .then(pl.col("value") / pl.col("denominator")).otherwise(None).alias("share"),
        _period_start_expr().alias("period_start"))
    df = df.with_columns(pl.col("share").clip(-1.0, 2.0))
    ids = issuer_ids(settings)
    if extra_ids is not None and not extra_ids.is_empty():
        ids = extra_ids if ids is None else pl.concat([ids, extra_ids], how="vertical_relaxed")
    if ids is not None and not ids.is_empty():
        df = df.join(ids.unique(["source_id", "entity_id"]), on=["source_id", "entity_id"], how="left")
    else:
        df = df.with_columns(pl.lit(None, pl.Utf8).alias("issuer_id"))
    now = now_utc()
    out = df.select("source_id", "entity_id", "issuer_id", "name", "filing_id", "form_type", "measure", "concept",
                    "period_start", "period_end", pl.col("qtrs").fill_null(0).cast(pl.Int16), "fiscal_period", "member_raw",
                    "member_type", "iso2", "region", "value", "currency", "denominator", "share", "n_countries",
                    "company_country", "knowledge_date", pl.lit(now).alias("ingested_at"))
    return out.unique(["source_id", "entity_id", "filing_id", "measure", "concept", "period_end", "qtrs", "member_raw",
                       "currency"], keep="first", maintain_order=True)


def _period_start_expr() -> pl.Expr:
    m = pl.col("period_end").dt.month().cast(pl.Int32) - 3 * pl.col("qtrs").cast(pl.Int32) + 1
    y = pl.col("period_end").dt.year().cast(pl.Int32) + ((m - 1).floordiv(12))
    mm = ((m - 1) % 12) + 1
    return pl.when(pl.col("qtrs").fill_null(0) > 0).then(pl.date(y, mm, 1)).otherwise(None)


def issuer_ids(settings: Settings | None = None) -> pl.DataFrame | None:
    """[source_id, entity_id, issuer_id]: CIK10 -> issuer (identifier_map cik, conf >= 0.9) for sec_fsds, and the
    fundamentals_raw source_entity_id mapping for IFRS sources."""
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        im = read_table("identifier_map", columns=["id_type", "id_value", "issuer_id", "confidence"], settings=settings)
    except Exception:  # noqa: BLE001
        return None
    cik = (im.filter((pl.col("id_type") == "cik") & (pl.col("confidence") >= 0.9))
           .with_columns(pl.col("id_value").str.strip_chars().str.zfill(10).alias("entity_id"))
           .sort("confidence", descending=True).unique("entity_id").select(pl.lit("sec_fsds").alias("source_id"),
                                                                            "entity_id", "issuer_id"))
    try:  # issuers.cik covers foreign filers (20-F / 40-F) whose identifier_map CIK rows are missing or < 0.9
        iss = read_table("issuers", columns=["issuer_id", "cik"], settings=settings).drop_nulls()
        iss = iss.filter(pl.col("cik").str.strip_chars().str.contains(r"^\d+$")).select(
            pl.lit("sec_fsds").alias("source_id"), pl.col("cik").str.strip_chars().str.zfill(10).alias("entity_id"),
            "issuer_id")
        cik = pl.concat([cik, iss], how="vertical_relaxed").unique("entity_id", keep="first")
    except Exception:  # noqa: BLE001
        pass
    return cik


def ifrs_dimensional(settings: Settings | None = None) -> pl.DataFrame:
    """Geographic revenue facts already in fundamentals_raw (non-SEC sources: ESEF, CMF, BIVA, ...)."""
    import duckdb  # noqa: PLC0415

    s = settings_of(settings)
    root = (s.data_dir / "normalized" / "fundamentals_raw").as_posix()
    con = duckdb.connect()
    con.execute("SET memory_limit = '1500MB'; SET threads = 4")
    try:
        df = con.sql(f"""
            select source_id, source_entity_id as entity_id, issuer_id, filing_id, form_type, concept, period_start,
                   period_end, value, unit, knowledge_date, dimensions_json
            from read_parquet('{root}/**/*.parquet', hive_partitioning=1, union_by_name=1)
            where source_id not in ('sec_edgar', 'sec_fsds') and dimensions_json is not null
              and dimensions_json ilike '%Geograph%Axis%'
              and (concept in ('Revenue', 'RevenueFromContractsWithCustomers', 'Revenues',
                               'RevenueFromContractWithCustomerExcludingAssessedTax', 'NoncurrentAssets')
                   -- roadmap #9: entity-specific revenue concepts on the geographic axis (not changes / costs)
                   or (regexp_matches(concept, '(?i)(revenue|sales|turnover)')
                       and not regexp_matches(concept, '(?i)(change|cost|deferred|unearned|receivable|tax|growth|'
                                                       || 'percentage|ratio|expense|liabilit|asset)')))
        """).pl()
    except Exception as e:  # noqa: BLE001
        log.warning("ifrs_dimensional: %s", e)
        return pl.DataFrame()
    finally:
        con.close()
    if df.is_empty():
        return df
    df = df.filter(pl.col("dimensions_json").str.count_matches('":"') == 1)  # single geographic axis only
    n_months = ((pl.col("period_end").dt.year() - pl.col("period_start").dt.year()) * 12
                + pl.col("period_end").dt.month() - pl.col("period_start").dt.month() + 1)
    cc = company_country_of_issuers(s)
    df = df.with_columns(
        pl.col("dimensions_json").str.extract('":"([^"]+)"').alias("member_raw"),
        pl.when(pl.col("concept") == "NoncurrentAssets").then(pl.lit("long_lived_assets")).otherwise(pl.lit("revenue"))
        .alias("measure"),
        pl.when(pl.col("period_start").is_null()).then(0).otherwise((n_months / 3).round()).cast(pl.Int16).alias("qtrs"),
        pl.col("issuer_id").replace_strict(cc, default=None, return_dtype=pl.Utf8).alias("company_country"))
    ids = df.select("source_id", "entity_id", "issuer_id").drop_nulls().unique(["source_id", "entity_id"])
    out = df.select("source_id", "entity_id", pl.lit(None, pl.Utf8).alias("name"), "filing_id", "form_type", "measure",
                    "concept", "member_raw", "period_end", "qtrs", "value", pl.col("unit").alias("currency"),
                    pl.lit(None, pl.Float64).alias("total"), "knowledge_date", "company_country",
                    pl.lit(None, pl.Utf8).alias("fiscal_period"))
    return finish(out, settings, extra_ids=ids)


def company_country_of_issuers(settings: Settings | None = None) -> dict[str, str]:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        iss = read_table("issuers", columns=["issuer_id", "country"], settings=settings)
    except Exception:  # noqa: BLE001
        return {}
    return {a: b for a, b in iss.iter_rows() if a and b}


def build(settings: Settings | None = None, ifrs: bool = True) -> dict[str, Any]:
    s = settings_of(settings)
    lz = landed(s)
    parts = []
    rep: dict[str, Any] = {}
    if lz is not None:
        num, sub = lz
        df = link_by_name(normalize_fsds(num.collect(), sub.collect(), s), s)
        parts.append(df)
        rep["sec_fsds_rows"] = df.height
    if ifrs:
        idf = ifrs_dimensional(s)
        parts.append(idf)
        rep["ifrs_rows"] = idf.height
        try:  # roadmap #9: ESEF segment-note tables (app.linkage.esef_geo, landed separately)
            from app.linkage.esef_geo import load_rows  # noqa: PLC0415

            edf = load_rows(s)
            if not edf.is_empty():
                parts.append(edf)
            rep["esef_textblock_rows"] = edf.height
        except Exception as e:  # noqa: BLE001
            log.warning("esef text-block rows unavailable: %s", e)
    parts = [p for p in parts if not p.is_empty()]
    if not parts:
        return rep
    df = pl.concat(parts, how="diagonal_relaxed")
    for c, dt in (("method", pl.Utf8), ("confidence", pl.Float64)):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dt).alias(c))
    df = df.with_columns(pl.col("method").fill_null("xbrl_dimensional"))
    df = df.with_columns(pl.when(pl.col("method") == "xbrl_dimensional").then(pl.lit(1.0)).otherwise(pl.col("confidence"))
                         .alias("confidence"))
    rep["rows"] = write(df, "company_geo_revenue", s, mode="overwrite")
    rep.update(coverage(df))
    return rep


def link_by_name(df: pl.DataFrame, settings: Settings | None = None, min_conf: float = 0.93) -> pl.DataFrame:
    """Non-US SEC filers without a CIK mapping -> issuer by exact (legal / alias) name match with OrgLinker
    (e.g. 'TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD' -> the TWSE-listed issuer)."""
    if df.is_empty():
        return df
    cand = df.filter(pl.col("issuer_id").is_null() & (pl.col("company_country").fill_null("") != "US")
                     & pl.col("name").is_not_null()).select("entity_id", "name").unique("entity_id")
    if cand.is_empty():
        return df
    try:
        from app.ingestion.gdelt_gkg import norm_org  # noqa: PLC0415
        from app.nlp.gdelt_link import OrgLinker  # noqa: PLC0415

        ol = OrgLinker.from_reference(settings)
    except Exception as e:  # noqa: BLE001
        log.warning("georev name linking unavailable: %s", e)
        return df
    m = {}
    for eid, nm in cand.iter_rows():
        iid, conf, method = ol.resolve(norm_org(nm))
        if iid and conf >= min_conf and method == "exact":
            m[eid] = iid
    if not m:
        return df
    return df.with_columns(pl.when(pl.col("issuer_id").is_null()).then(
        pl.col("entity_id").replace_strict(m, default=None, return_dtype=pl.Utf8)).otherwise(pl.col("issuer_id"))
        .alias("issuer_id"))


def coverage(df: pl.DataFrame) -> dict[str, Any]:
    rev = df.filter((pl.col("measure") == "revenue") & pl.col("member_type").is_in(["country", "domestic"])
                    & pl.col("iso2").is_not_null())
    ent = rev.select("source_id", "entity_id", "company_country", "issuer_id").unique(["source_id", "entity_id"])
    return {"entities_with_country_revenue": ent.height,
            "entities_non_us": ent.filter(pl.col("company_country").fill_null("") != "US").height,
            "issuers_linked": ent.get_column("issuer_id").drop_nulls().n_unique(),
            "issuers_linked_non_us": ent.filter(pl.col("company_country").fill_null("") != "US")
            .get_column("issuer_id").drop_nulls().n_unique(),
            "filings": rev.get_column("filing_id").n_unique(),
            "first_period": str(rev.get_column("period_end").min()) if rev.height else None}


def refresh_non_sec(settings: Settings | None = None) -> dict[str, Any]:
    """Rebuild only the non-SEC partitions (IFRS dimensional facts + ESEF text-block tables) without re-reading the
    FSDS landing (bounded memory; the table is partitioned by source_id). Roadmap #9."""
    from app.linkage.esef_geo import load_rows  # noqa: PLC0415

    s = settings_of(settings)
    parts = [ifrs_dimensional(s), load_rows(s)]
    parts = [p for p in parts if not p.is_empty()]
    if not parts:
        return {"rows": 0}
    df = pl.concat(parts, how="diagonal_relaxed")
    for c, dt in (("method", pl.Utf8), ("confidence", pl.Float64)):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dt).alias(c))
    df = df.with_columns(pl.col("method").fill_null("xbrl_dimensional"))
    df = df.with_columns(pl.when(pl.col("method") == "xbrl_dimensional").then(pl.lit(1.0)).otherwise(pl.col("confidence"))
                         .alias("confidence"))
    n = write(df, "company_geo_revenue", s, mode="overwrite_partition")
    return {"rows": n, "sources": dict(df.group_by("source_id").len().iter_rows()), **coverage(df)}


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    s = ctx.settings
    if str(p.get("non_sec_only", "false")).lower() == "true":
        rep = refresh_non_sec(s)
        return JobResult(status="success", rows_written={"company_geo_revenue": rep.get("rows", 0)}, message=str(rep))
    lrep = land(s, start=str(p.get("start", "2009q1")), max_quarters=int(p.get("max_quarters", 999)))
    brep = build(s)
    update_progress("A5", "done" if brep.get("rows") else "partial", settings=s, **{**lrep, **brep})
    status = "success" if not lrep["failed"] else "partial"
    return JobResult(status=status, rows_written={"company_geo_revenue": brep.get("rows", 0)},
                     message=str({k: (v if not isinstance(v, list) else len(v)) for k, v in {**lrep, **brep}.items()}))


if __name__ == "__main__":  # detached landing: python -m app.linkage.georev [start] [max_quarters]
    import sys

    from app.core.logsetup import setup_logging  # noqa: PLC0415

    try:
        setup_logging()
    except Exception:  # noqa: BLE001
        pass
    a = sys.argv[1:]
    r = land(start=a[0] if a else "2009q1", max_quarters=int(a[1]) if len(a) > 1 else 999)
    print({k: (v if not isinstance(v, list) else v) for k, v in r.items()})
