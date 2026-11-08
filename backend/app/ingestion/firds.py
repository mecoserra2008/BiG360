"""ESMA / FCA FIRDS reference data + ISO 10383 MIC registry (coverage v2 W2, docs/COVERAGE_V2_PLAN.md).

Sources (public bulk files, no keys, own User-Agent, <= 1 request/s):
  * ESMA FIRDS  https://registers.esma.europa.eu/solr/esma_registers_firds_files/select (file list, Solr JSON)
                -> FULINS_E_<date>_NNofMM.zip (weekly full file of every equity instrument admitted to trading on an
                EEA trading venue, one RefData record per (ISIN, venue)).
  * FCA FIRDS   https://api.data.fca.org.uk/fca_data_firds_files (file list, Elasticsearch JSON) ->
                https://data.fca.org.uk/artefacts/FIRDS/FULINS_E_<date>_01of01.zip (same ISO 20022 auth.017 layout,
                UK trading venues since 2021). robots.txt of data.fca.org.uk: `User-agent: * Allow: /`,
                content signal use=reference.
  * ISO 10383   https://www.iso20022.org/sites/default/files/ISO10383_MIC/ISO10383_MIC.csv: MIC -> operating MIC,
                segment flag, MARKET CATEGORY CODE (RMKT = regulated market, MLTF = MTF, ...), country, status.
  * ESMA upreg  https://registers.esma.europa.eu/solr/esma_registers_upreg (ae_entityTypeCode MIR = regulated market,
                ae_micLeiEsmaId = its MIC): the regulator's own list of EEA regulated markets.

A venue is a regulated market when ISO 10383 classifies that exact MIC as RMKT, or ESMA lists it as a regulated
market (MIR). Operating MICs classified NSPD (XETR, XFRA, XMIL, XWBO, ...) are not regulated markets by themselves:
FIRDS reports RM admissions on the segment MIC (XETA, FRAA, MTAA, WBAH).

Parsed equity records (CFI category E) are kept as Parquet (zstd, dictionary strings) under
data/reference/regulatory/firds_<source>_equity.parquet. Raw zips are deleted once parsed; the processed file names
are tracked in data/state/regulatory_register.json (`firds_processed`).
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

ESMA_FILES = "https://registers.esma.europa.eu/solr/esma_registers_firds_files/select"
FCA_FILES = "https://api.data.fca.org.uk/fca_data_firds_files"
ISO_MIC_CSV = "https://www.iso20022.org/sites/default/files/ISO10383_MIC/ISO10383_MIC.csv"
ESMA_UPREG = "https://registers.esma.europa.eu/solr/esma_registers_upreg/select"

#: CFI equity groups kept (shares, preferred, convertibles, partnership units, depositary receipts); funds (CI*) and
#: rights / structured products are not issuer equity
EQUITY_CFI = ("ES", "EP", "EC", "EF", "EL", "ED")
#: CFI groups that make an issuer part of a register (depositary receipts are issued by the depositary bank)
ISSUER_CFI = ("ES", "EP", "EC", "EF", "EL")
#: EEA member states (ESMA FIRDS scope)
EEA = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE IS LI NO".split())

FIRDS_SCHEMA: dict[str, Any] = {
    "isin": pl.Utf8, "full_name": pl.Utf8, "short_name": pl.Utf8, "cfi": pl.Utf8, "currency": pl.Utf8,
    "issuer_lei": pl.Utf8, "venue_mic": pl.Utf8, "issuer_request": pl.Boolean, "admission_approved": pl.Date,
    "admission_requested": pl.Date, "first_trade": pl.Date, "termination": pl.Date, "relevant_venue": pl.Utf8,
    "relevant_authority": pl.Utf8,
}
MIC_SCHEMA: dict[str, Any] = {
    "mic": pl.Utf8, "operating_mic": pl.Utf8, "is_segment": pl.Boolean, "name": pl.Utf8, "category": pl.Utf8,
    "country": pl.Utf8, "status": pl.Utf8, "esma_rm": pl.Boolean, "is_rm": pl.Boolean,
}


def reg_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).data_dir / "reference" / "regulatory"
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_parquet(df: pl.DataFrame, p: Path) -> None:
    """Atomic zstd Parquet write (dictionary encoding is pyarrow's default for strings)."""
    tmp = p.with_suffix(p.suffix + ".tmp")
    df.write_parquet(tmp, compression="zstd", compression_level=9, statistics=True)
    tmp.replace(p)


def _d(s: str | None) -> date | None:
    if not s:
        return None
    try:
        y = int(s[:4])
        if y >= 9999:
            return date(9999, 12, 31)
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------ parsing (pure)


def iter_firds_records(fh: Any, cfi_prefixes: tuple[str, ...] = EQUITY_CFI) -> Iterator[dict[str, Any]]:
    """Stream RefData records of a FIRDS FULINS XML file object (lxml iterparse, constant memory)."""
    from lxml import etree

    for _, el in etree.iterparse(fh, events=("end",), tag="{*}RefData", huge_tree=True):
        g = el.find("{*}FinInstrmGnlAttrbts")
        cfi = (g.findtext("{*}ClssfctnTp") if g is not None else None) or ""
        if g is not None and cfi[:2] in cfi_prefixes:
            v = el.find("{*}TradgVnRltdAttrbts")
            t = el.find("{*}TechAttrbts")

            def vt(tag: str, node: Any = v) -> str | None:
                return node.findtext(tag) if node is not None else None

            req = vt("{*}IssrReq")
            yield {
                "isin": g.findtext("{*}Id"), "full_name": g.findtext("{*}FullNm"), "short_name": g.findtext("{*}ShrtNm"),
                "cfi": cfi, "currency": g.findtext("{*}NtnlCcy"), "issuer_lei": el.findtext("{*}Issr"),
                "venue_mic": vt("{*}Id"), "issuer_request": None if req is None else req.strip().lower() == "true",
                "admission_approved": _d(vt("{*}AdmssnApprvlDtByIssr")), "admission_requested": _d(vt("{*}ReqForAdmssnDt")),
                "first_trade": _d(vt("{*}FrstTradDt")), "termination": _d(vt("{*}TermntnDt")),
                "relevant_venue": vt("{*}RlvntTradgVn", t), "relevant_authority": vt("{*}RlvntCmptntAuthrty", t),
            }
        el.clear()
        while el.getprevious() is not None:
            del el.getparent()[0]


def parse_firds_zip(path: Path, cfi_prefixes: tuple[str, ...] = EQUITY_CFI) -> pl.DataFrame:
    """One FULINS zip (single XML member) -> equity venue records (FIRDS_SCHEMA)."""
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.lower().endswith(".xml"):
                continue
            with z.open(name) as fh:
                rows.extend(iter_firds_records(fh, cfi_prefixes))
    return pl.DataFrame(rows, schema=FIRDS_SCHEMA) if rows else pl.DataFrame(schema=FIRDS_SCHEMA)


def parse_mic_csv(content: bytes, esma_rm_mics: set[str] | None = None) -> pl.DataFrame:
    """ISO 10383 CSV -> MIC_SCHEMA (is_rm = ISO RMKT on that MIC, or listed by ESMA as a regulated market)."""
    import io

    df = pl.read_csv(io.BytesIO(content), infer_schema_length=0, encoding="utf8-lossy")
    col = {c.upper(): c for c in df.columns}

    def g(name: str) -> pl.Expr:
        return pl.col(col[name]) if name in col else pl.lit(None, pl.Utf8)

    esma = {m.upper() for m in (esma_rm_mics or set())}
    out = df.select(
        g("MIC").str.strip_chars().str.to_uppercase().alias("mic"),
        g("OPERATING MIC").str.strip_chars().str.to_uppercase().alias("operating_mic"),
        (g("OPRT/SGMT").str.strip_chars().str.to_uppercase() == "SGMT").alias("is_segment"),
        g("MARKET NAME-INSTITUTION DESCRIPTION").alias("name"),
        g("MARKET CATEGORY CODE").str.strip_chars().str.to_uppercase().alias("category"),
        g("ISO COUNTRY CODE (ISO 3166)").str.strip_chars().str.to_uppercase().alias("country"),
        g("STATUS").str.strip_chars().str.to_uppercase().alias("status"),
    ).filter(pl.col("mic").is_not_null())
    out = out.with_columns(pl.col("mic").is_in(list(esma)).alias("esma_rm"))
    out = out.with_columns(((pl.col("category") == "RMKT") | pl.col("esma_rm")).alias("is_rm"))
    return out.unique("mic", keep="first").select(list(MIC_SCHEMA))


def parse_esma_rm(docs: list[dict[str, Any]]) -> set[str]:
    """ESMA upreg MIR documents -> set of MICs of active regulated markets."""
    out: set[str] = set()
    for d in docs:
        mic = (d.get("ae_micLeiEsmaId") or "").strip().upper()
        if len(mic) == 4 and (d.get("ae_status") or "Active").lower() == "active":
            out.add(mic)
    return out


# ------------------------------------------------------------------------------------------------ network


def _client(source_id: str, settings: Settings | None = None):
    from app.core.http import HttpClient
    from app.core.ratelimit import RateLimit

    return HttpClient(source_id, settings=settings or get_settings(), rate_limit=RateLimit(1.0, 1))


def mic_registry(settings: Settings | None = None, max_age_days: int = 30, http: Any = None) -> pl.DataFrame:
    """ISO 10383 + ESMA regulated-market list, cached `max_age_days` in reference/regulatory/mic_registry.parquet."""
    s = settings or get_settings()
    p = reg_dir(s) / "mic_registry.parquet"
    if p.exists() and datetime.fromtimestamp(p.stat().st_mtime, UTC) > datetime.now(UTC) - timedelta(days=max_age_days):
        return pl.read_parquet(p)
    h = http or _client("esma", s)
    esma: set[str] = set()
    try:
        j = h.get(ESMA_UPREG, params={"q": "*:*", "fq": "ae_entityTypeCode:MIR", "rows": 1000, "wt": "json"},
                  cache_ttl=7 * 86400).json()
        esma = parse_esma_rm(j["response"]["docs"])
    except Exception as e:  # noqa: BLE001 - ISO alone still classifies every RM segment
        log.warning("ESMA regulated-market list unavailable: %s", e)
    try:
        content = h.get(ISO_MIC_CSV, cache_ttl=7 * 86400).content
    except Exception as e:  # noqa: BLE001
        if p.exists():
            log.warning("ISO 10383 CSV unavailable (%s): keeping the cached registry", e)
            return pl.read_parquet(p)
        raise
    df = parse_mic_csv(content, esma)
    write_parquet(df, p)
    log.info("MIC registry: %d MICs, %d regulated markets (%d ESMA-listed)", df.height, df["is_rm"].sum(), len(esma))
    return df


def list_full_files(source: str, settings: Settings | None = None, http: Any = None, lookback_days: int = 15) -> list[dict[str, str]]:
    """Latest weekly FULINS_E file set of `source` ('esma' | 'fca'): [{file_name, url, publication_date}]."""
    s = settings or get_settings()
    end = datetime.now(UTC).date()
    start = end - timedelta(days=lookback_days)
    if source == "esma":
        h = http or _client("esma", s)
        q = {"q": "*", "fq": f"publication_date:[{start.isoformat()}T00:00:00Z TO {end.isoformat()}T23:59:59Z]",
             "wt": "json", "rows": 500}
        docs = h.get(ESMA_FILES, params=q, cache_ttl=6 * 3600).json()["response"]["docs"]
        files = [{"file_name": d["file_name"], "url": d["download_link"], "publication_date": str(d.get("publication_date"))[:10]}
                 for d in docs if str(d.get("file_name", "")).startswith("FULINS_E_")]
    elif source == "fca":
        h = http or _client("fca_firds", s)
        q = {"q": f"((file_type:FULINS) AND (publication_date:[{start.isoformat()} TO {end.isoformat()}]))",
             "from": 0, "size": 100}
        hits = h.get(FCA_FILES, params=q, cache_ttl=6 * 3600).json()["hits"]["hits"]
        files = [{"file_name": x["_source"]["file_name"], "url": x["_source"]["download_link"],
                  "publication_date": x["_source"].get("publication_date")} for x in hits
                 if str(x["_source"].get("file_name", "")).startswith("FULINS_E_")]
    else:
        raise ValueError(source)
    if not files:
        return []
    latest = max(f["file_name"].split("_")[2] for f in files)
    return sorted([f for f in files if f["file_name"].split("_")[2] == latest], key=lambda f: f["file_name"])


def firds_equity(source: str, settings: Settings | None = None, state: dict[str, Any] | None = None,
                 http: Any = None, force: bool = False) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Latest FIRDS equity venue records of `source`, parsed once per weekly file set.

    Returns (frame, info). The parsed frame is cached in reference/regulatory/firds_<source>_equity.parquet; when the
    newest file set equals `state['firds_processed'][source]['files']` nothing is downloaded. Raw zips land under
    raw/<esma_firds|fca_firds>/fulins_e/ and are deleted right after parsing."""
    s = settings or get_settings()
    out_p = reg_dir(s) / f"firds_{source}_equity.parquet"
    st = state.setdefault("firds_processed", {}) if state is not None else {}
    prev = st.get(source) or {}
    try:
        files = list_full_files(source, s, http)
    except Exception as e:  # noqa: BLE001
        if out_p.exists():
            log.warning("FIRDS %s file list unavailable (%s): using the parsed file of %s", source, e, prev.get("set"))
            return pl.read_parquet(out_p), {"source": source, "status": "cached", "error": str(e)[:200], **prev}
        raise
    names = [f["file_name"] for f in files]
    if not force and out_p.exists() and names and prev.get("files") == names:
        return pl.read_parquet(out_p), {"source": source, "status": "unchanged", **prev}
    if not names:
        if out_p.exists():
            return pl.read_parquet(out_p), {"source": source, "status": "no_new_file", **prev}
        raise RuntimeError(f"FIRDS {source}: no FULINS_E file in the last weeks")
    h = http or _client("esma" if source == "esma" else "fca_firds", s)
    raw = s.raw_dir / ("esma_firds" if source == "esma" else "fca_firds") / "fulins_e"
    raw.mkdir(parents=True, exist_ok=True)
    frames = []
    for f in files:
        dest = raw / f["file_name"]
        if not dest.exists() or dest.stat().st_size < 1000:
            h.download(f["url"], dest)
        frames.append(parse_firds_zip(dest))
        log.info("FIRDS %s %s: %d equity venue records", source, f["file_name"], frames[-1].height)
    df = pl.concat(frames, how="vertical") if frames else pl.DataFrame(schema=FIRDS_SCHEMA)
    df = df.unique(["isin", "venue_mic"], keep="last")
    write_parquet(df, out_p)
    for f in files:  # processed -> raw zip no longer needed (the parsed Parquet is the durable copy)
        (raw / f["file_name"]).unlink(missing_ok=True)
    info = {"files": names, "set": names[0].split("_")[2], "rows": df.height,
            "processed_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if state is not None:
        st[source] = info
    return df, {"source": source, "status": "parsed", **info}


# ------------------------------------------------------------------------------------------------ register helpers


def rm_admissions(firds: pl.DataFrame, mics: pl.DataFrame, today: date | None = None,
                  countries: frozenset[str] | None = None, issuer_request_only: bool = True) -> pl.DataFrame:
    """Records on a regulated-market venue that are live (no termination before today) for issuer equity CFIs.

    countries: keep venues of these MIC countries (EEA for ESMA, {'GB'} for FCA)."""
    today = today or datetime.now(UTC).date()
    rm = mics.filter(pl.col("is_rm"))
    if countries is not None:
        rm = rm.filter(pl.col("country").is_in(list(countries)))
    f = firds.filter(pl.col("cfi").str.slice(0, 2).is_in(list(ISSUER_CFI))
                     & pl.col("venue_mic").is_in(rm.get_column("mic").to_list())
                     & (pl.col("termination").is_null() | (pl.col("termination") > today)))
    if issuer_request_only:
        f = f.filter(pl.col("issuer_request").fill_null(False))
    return f.join(rm.select(pl.col("mic").alias("venue_mic"), pl.col("country").alias("venue_country"),
                            pl.col("name").alias("venue_name"), "operating_mic"), on="venue_mic", how="left")
