"""EU / European universe expansion driver (docs/EU_EXPANSION_PLAN.md) - resumable, detached-friendly.

    python -m app.ingestion.eu_expansion census      # step 1: ESEF filer census + FIRDS + GLEIF ISIN-LEI + DILA/AFM
    python -m app.ingestion.eu_expansion identity    # step 2: candidates -> OpenFIGI (cached, chunked) -> yf symbol check
    python -m app.ingestion.eu_expansion universe    # step 3: liquidity + yfinance market cap -> seeds/eu_broad.csv
    python -m app.ingestion.eu_expansion status      # state + running PIDs

Every step persists its cursor / intermediate output under data/state/eu_expansion*.json and
data/reference/eu_census/, so a re-run resumes where it stopped (pages / chunks / batches already done are skipped).
Each step writes data/logs/eu_expansion/<step>.pid while running and logs to data/logs/eu_expansion/<step>.log.

Sources (no keys): filings.xbrl.org JSON:API, ESMA FIRDS FULINS_E (identity_universe.fetch_firds_equities), GLEIF
ISIN-LEI daily bulk file (mapping.gleif.org), DILA info-financiere ODS group_by export, AFM register XML export,
OpenFIGI /v3/mapping (identity_figi.map_jobs, cached), yfinance (download + .info, shared limiters).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings

log = logging.getLogger("app.ingestion.eu_expansion")

#: ISIN country prefix -> issuer country used for the universe (Crown dependencies -> GB, FO -> DK, LI -> CH)
EUROPE_ISIN: dict[str, str] = {c: c for c in (
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE GB CH NO IS").split()}
EUROPE_ISIN.update({"JE": "GB", "GG": "GB", "IM": "GB", "FO": "DK", "LI": "CH"})

#: Bloomberg exchange code (OpenFIGI exchCode) -> (operating MIC, yfinance suffix). Primary regulated venues only.
BBG_VENUE: dict[str, tuple[str, str]] = {
    "FP": ("XPAR", ".PA"), "NA": ("XAMS", ".AS"), "BB": ("XBRU", ".BR"), "PL": ("XLIS", ".LS"), "ID": ("XDUB", ".IR"),
    "GY": ("XETR", ".DE"), "IM": ("XMIL", ".MI"), "SM": ("XMAD", ".MC"), "SQ": ("XMAD", ".MC"), "SE": ("XSWX", ".SW"),
    "SW": ("XSWX", ".SW"), "VX": ("XSWX", ".SW"), "SS": ("XSTO", ".ST"), "DC": ("XCSE", ".CO"), "FH": ("XHEL", ".HE"),
    "NO": ("XOSL", ".OL"), "AV": ("XWBO", ".VI"), "PW": ("XWAR", ".WA"), "GA": ("XATH", ".AT"), "LN": ("XLON", ".L"),
    "CP": ("XPRA", ".PR"), "HB": ("XBUD", ".BD"), "RE": ("XBSE", ".RO"), "IR": ("XICE", ".IC"), "ET": ("XTAL", ".TL"),
    "LR": ("XRIS", ".RG"), "LH": ("XLIT", ".VS"),
}
#: issuer country -> home exchange codes (preference order)
HOME_BBG: dict[str, tuple[str, ...]] = {
    "FR": ("FP",), "NL": ("NA",), "BE": ("BB",), "PT": ("PL",), "IE": ("ID", "LN"), "DE": ("GY",), "IT": ("IM",),
    "ES": ("SQ", "SM"), "CH": ("SE", "SW", "VX"), "SE": ("SS",), "DK": ("DC",), "FI": ("FH",), "NO": ("NO",),
    "AT": ("AV",), "PL": ("PW",), "GR": ("GA",), "GB": ("LN",), "CZ": ("CP",), "HU": ("HB",), "RO": ("RE",),
    "IS": ("IR",), "EE": ("ET",), "LV": ("LR",), "LT": ("LH",),
}
#: FIRDS 'most relevant trading venue' (segment or operating MIC) -> exchange code, for issuers listed abroad
#: (e.g. NL/LU-incorporated companies listed in Paris or Milan). MTFs are deliberately absent.
FIRDS_VENUE_BBG: dict[str, str] = {
    "XPAR": "FP", "XAMS": "NA", "XBRU": "BB", "XLIS": "PL", "XMSM": "ID", "XDUB": "ID", "XESM": "ID", "XETR": "GY",
    "XETA": "GY", "XETB": "GY", "XETS": "GY", "XFRA": "GY", "FRAA": "GY", "MTAA": "IM", "XMIL": "IM", "EXGM": "IM",
    "XMAD": "SQ", "XMCE": "SQ", "XSTO": "SS", "SSME": "SS", "XCSE": "DC", "DCSE": "DC", "XHEL": "FH", "FNFI": "FH",
    "XOSL": "NO", "XOAS": "NO", "XWBO": "AV", "WBAH": "AV", "XWAR": "PW", "XATH": "GA", "XPRA": "CP", "XBUD": "HB",
    "XBSE": "RE", "XICE": "IR", "XTAL": "ET", "XRIS": "LR", "XLIT": "LH", "XLON": "LN", "XSWX": "SE", "XVTX": "SE",
}
_CLASS_SUFFIX_BBG = {"SS", "DC", "FH", "NO", "IR"}  # Nordic tickers: Bloomberg 'ERICB' <-> Yahoo 'ERIC-B'
_EQUITY_TYPES = {"Common Stock", "REIT", "Ordinary Shares", "Dutch Cert", "NY Reg Shrs", "Stapled Security"}

MIN_MCAP_EUR = 300e6
MIN_ADV_EUR = 50e3          # median daily traded value (last ~60 sessions) for "liquid prices"
PREFILTER_ADV_EUR = 20e3    # loose pre-filter before the (slow) per-ticker yfinance .info calls


# ------------------------------------------------------------------------------------------------ plumbing


def _s(settings: Settings | None = None) -> Settings:
    return settings or get_settings()


def census_dir(settings: Settings | None = None) -> Path:
    p = _s(settings).data_dir / "reference" / "eu_census"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir(settings: Settings | None = None) -> Path:
    p = _s(settings).data_dir / "logs" / "eu_expansion"
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(settings: Settings | None = None) -> Path:
    return _s(settings).data_dir / "state" / "eu_expansion.json"


def load_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"steps": {}}


def update_state(step: str, settings: Settings | None = None, **fields: Any) -> dict[str, Any]:
    """Merge `fields` into state['steps'][step] (atomic write)."""
    st = load_state(settings)
    cur = st.setdefault("steps", {}).setdefault(step, {})
    cur.update(fields)
    cur["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)
    return st


def _write_parquet(df: pl.DataFrame, p: Path) -> None:
    tmp = p.with_suffix(".tmp")
    df.write_parquet(tmp)
    tmp.replace(p)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_steps(settings: Settings | None = None) -> dict[str, int]:
    out = {}
    for p in log_dir(settings).glob("*.pid"):
        try:
            pid = int(p.read_text().strip())
        except (OSError, ValueError):
            continue
        if pid_alive(pid):
            out[p.stem] = pid
    return out


# ------------------------------------------------------------------------------------------------ pure helpers


def issuer_country(isin: str | None) -> str | None:
    return EUROPE_ISIN.get((isin or "")[:2])


def bbg_to_yf_candidates(ticker: str, exch: str) -> list[str]:
    """Bloomberg ticker + exchange code -> Yahoo symbol candidates (first = most likely)."""
    v = BBG_VENUE.get(exch)
    if not v or not ticker:
        return []
    t = ticker.strip().upper().replace(" ", "-").replace("/", "-").rstrip("-")
    out = [t + v[1]]
    if exch in _CLASS_SUFFIX_BBG and len(t) >= 3 and t[-1] in "ABCDR" and "-" not in t:
        out.append(f"{t[:-1]}-{t[-1]}{v[1]}")
    return out


def pick_home_listing(records: list[dict[str, Any]], country: str | None, firds_venue: str | None) -> dict[str, Any] | None:
    """Choose the primary-venue OpenFIGI record of an ISIN: home exchange, else the FIRDS most relevant venue (when it
    is a regulated primary market), else None (only MTF / foreign listings -> not a primary European listing)."""
    eq = [r for r in records or [] if r.get("marketSector") == "Equity" and (r.get("securityType") in _EQUITY_TYPES
                                                                              or r.get("securityType2") == "Common Stock")]
    by_ex: dict[str, dict[str, Any]] = {}
    for r in eq:
        ex = r.get("exchCode")
        if ex in BBG_VENUE and r.get("ticker") and ex not in by_ex:
            by_ex[ex] = r
    prefs = list(HOME_BBG.get(country or "", ()))
    fv = FIRDS_VENUE_BBG.get((firds_venue or "").upper())
    if fv:
        prefs.append(fv)
        if fv == "SQ":
            prefs.append("SM")
        if fv == "SE":
            prefs += ["SW", "VX"]
    for ex in prefs:
        if ex in by_ex:
            return {**by_ex[ex], "_exch": ex}
    return None


# ------------------------------------------------------------------------------------------------ step 1: census


def esef_census(settings: Settings | None = None, page_size: int = 500) -> pl.DataFrame:
    """Every filing on filings.xbrl.org (paged, date_added ascending => stable pages; cursor = next page)."""
    from app.core.http import HttpClient

    s = _s(settings)
    raw = s.raw_dir / "esef_xbrl_org" / "census"
    raw.mkdir(parents=True, exist_ok=True)
    st = load_state(s).get("steps", {}).get("1_census", {})
    page = int(st.get("esef_next_page", 1))
    h = HttpClient("esef_xbrl_org", settings=s)
    try:
        while True:
            j = h.get("https://filings.xbrl.org/api/filings", params={
                "page[size]": page_size, "page[number]": page, "include": "entity", "sort": "date_added"},
                cache_ttl=0).json()
            data = j.get("data") or []
            ents = {e["id"]: (e.get("attributes") or {}) for e in j.get("included") or [] if e.get("type") == "entity"}
            rows = []
            for f in data:
                a = f.get("attributes") or {}
                rel = ((f.get("relationships") or {}).get("entity") or {}).get("data") or {}
                e = ents.get(rel.get("id")) or {}
                rows.append({"fxo_id": a.get("fxo_id"), "lei": e.get("identifier"), "entity_name": e.get("name"),
                             "country": a.get("country"), "period_end": a.get("period_end"),
                             "date_added": a.get("date_added"), "json_url": a.get("json_url"),
                             "error_count": a.get("error_count")})
            if not data:
                # the last page with data is re-read on the next run; earlier pages are final (ascending date_added)
                update_state("1_census", s, esef_next_page=max(1, page - 1), esef_total=(j.get("meta") or {}).get("count"))
                break
            (raw / f"page_{page:05d}.json").write_text(json.dumps(rows), encoding="utf-8")
            log.info("esef census page %d: %d filings", page, len(data))
            page += 1
            update_state("1_census", s, esef_next_page=page)
    finally:
        h.close()
    rows = []
    for p in sorted(raw.glob("page_*.json")):
        rows += json.loads(p.read_text(encoding="utf-8"))
    df = pl.DataFrame(rows, schema={"fxo_id": pl.Utf8, "lei": pl.Utf8, "entity_name": pl.Utf8, "country": pl.Utf8,
                                    "period_end": pl.Utf8, "date_added": pl.Utf8, "json_url": pl.Utf8,
                                    "error_count": pl.Int64}).unique("fxo_id", keep="last")
    _write_parquet(df, census_dir(s) / "esef_filings.parquet")
    return df


def gleif_isin_lei(settings: Settings | None = None, max_age_days: int = 7) -> pl.DataFrame:
    """GLEIF ISIN<->LEI daily bulk file (no API calls), restricted to European ISIN prefixes. Cached 7 days."""
    from app.core.http import HttpClient

    s = _s(settings)
    out_p = census_dir(s) / "gleif_isin_lei_eu.parquet"
    if out_p.exists() and datetime.fromtimestamp(out_p.stat().st_mtime, UTC) > datetime.now(UTC) - timedelta(days=max_age_days):
        return pl.read_parquet(out_p)
    h = HttpClient("gleif", settings=s)
    try:
        meta = h.get("https://mapping.gleif.org/api/v2/isin-lei/latest", cache_ttl=3600).json()
        link = _find_key(meta, "downloadLink")
        if not link:
            raise ValueError("GLEIF isin-lei: no downloadLink in response")
        dest = s.raw_dir / "gleif" / "isin_lei" / link.rstrip("/").rsplit("/", 1)[-1]
        if not dest.suffix:
            dest = dest.with_suffix(".zip")
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            h.download(link, dest)
    finally:
        h.close()
    with zipfile.ZipFile(dest) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        data = z.read(name)
    df = pl.read_csv(io.BytesIO(data), infer_schema_length=0)
    df = df.rename({c: c.strip().upper() for c in df.columns}).select(pl.col("LEI").alias("lei"), pl.col("ISIN").alias("isin"))
    df = df.filter(pl.col("isin").str.slice(0, 2).is_in(list(EUROPE_ISIN)))
    _write_parquet(df, out_p)
    log.info("GLEIF isin-lei: %d European ISINs (%s)", df.height, dest.name)
    return df


def _find_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            got = _find_key(v, key)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_key(v, key)
            if got:
                return got
    return None


def dila_census(settings: Settings | None = None) -> pl.DataFrame:
    """DILA info-financiere (French OAM): ESEF package deposits per LEI (one ODS group_by export call)."""
    from app.core.http import HttpClient

    s = _s(settings)
    h = HttpClient("dila_fr", settings=s)
    base = "https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod"
    where = '(fichierdecontenu_inf_fic_nom like "*.zip" or fichierdecontenu_inf_fic_nom like "*.xbri")'
    try:
        j = h.get(base + "/exports/json", params={
            "select": "identificationsociete_iso_cd_lei as lei, count(*) as n, max(informationdeposee_inf_dat_emt) as last",
            "where": where, "group_by": "identificationsociete_iso_cd_lei"}, cache_ttl=24 * 3600).json()
    finally:
        h.close()
    df = pl.DataFrame([r for r in j if r.get("lei")], schema={"lei": pl.Utf8, "n": pl.Int64, "last": pl.Utf8})
    return df.rename({"n": "dila_n", "last": "dila_last"})


def afm_census(settings: Settings | None = None) -> pl.DataFrame:
    """AFM financial-reporting register (Dutch OAM): full export, grouped by issuer (LEI taken from ESEF file names)."""
    import app.ingestion.afm_nl as afm

    a = afm.AfmNlAdapter(settings=_s(settings))
    rows = a.export_rows()
    lei_re = re.compile(r"\b([0-9A-Z]{18}[0-9]{2})\b")
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if "annual" not in (r.get("type") or "").lower():
            continue
        o = out.setdefault(r["issuer"], {"afm_issuer": r["issuer"], "lei": None, "afm_n": 0, "afm_last_fy": None})
        o["afm_n"] += 1
        fy = r.get("boekjaar") or None
        if fy and (o["afm_last_fy"] is None or fy > o["afm_last_fy"]):
            o["afm_last_fy"] = fy
        m = lei_re.search((r.get("filename") or "").upper())
        if m and not o["lei"]:
            o["lei"] = m.group(1)
    return pl.DataFrame(list(out.values()), schema={"afm_issuer": pl.Utf8, "lei": pl.Utf8, "afm_n": pl.Int64,
                                                     "afm_last_fy": pl.Utf8})


def fca_census(settings: Settings | None = None, since: str = "2021-01-01") -> pl.DataFrame:
    """FCA NSM (UK OAM): every ESEF-tagged annual report (type_code ACS with an xBRL-JSON rendition) since 2021,
    grouped by LEI (market-wide search, ~500 hits per request)."""
    import app.ingestion.fca_nsm as nsm

    a = nsm.FcaNsmAdapter(settings=_s(settings))
    # NSM rejects size=500 ("Unable to search the data", HTTP 404) and caps from+size near 10k: yearly windows of 100
    hits: list[dict[str, Any]] = []
    y0 = date.fromisoformat(since).year
    for y in range(y0, datetime.now(UTC).year + 1):
        hits += a.search([{"name": "type_code", "value": ["ACS"]}], date(y, 1, 1), until=date(y, 12, 31), size=100,
                         max_hits=9900)
    rows = [{"lei": h.get("lei"), "company": h.get("company"), "pub": str(h.get("publication_date") or "")[:10],
             "esef": bool(h.get("json_link"))} for h in hits if h.get("lei")]
    df = pl.DataFrame(rows, schema={"lei": pl.Utf8, "company": pl.Utf8, "pub": pl.Utf8, "esef": pl.Boolean})
    return df.group_by("lei").agg(pl.col("company").first().alias("fca_company"), pl.len().alias("fca_n_acs"),
                                  pl.col("esef").sum().alias("fca_n_esef"), pl.col("pub").max().alias("fca_last"))


def step_census(settings: Settings | None = None) -> dict[str, Any]:
    from app.normalize.identity_universe import fetch_firds_equities

    s = _s(settings)
    t0 = time.time()
    esef = esef_census(s)
    firds = fetch_firds_equities(s)
    gle = gleif_isin_lei(s)
    parts: dict[str, Any] = {}
    for name, fn in (("dila", dila_census), ("afm", afm_census), ("fca", fca_census)):
        try:
            parts[name] = fn(s)
        except Exception as e:  # noqa: BLE001 - census of one archive must not fail the step
            log.warning("census %s failed: %s", name, e)
            parts[name] = None
    # ---- per-LEI table
    e = (esef.filter(pl.col("lei").is_not_null())
         .group_by("lei").agg(pl.col("entity_name").last(), pl.col("country").mode().first().alias("esef_country"),
                              pl.len().alias("esef_n_filings"), pl.col("period_end").max().alias("esef_latest_period"),
                              pl.col("date_added").max().alias("esef_last_added")))
    f = (firds.filter(pl.col("cfi").str.starts_with("ES") & pl.col("issuer_lei").is_not_null()
                      & pl.col("isin").str.slice(0, 2).is_in(list(EUROPE_ISIN)))
         .group_by(pl.col("issuer_lei").alias("lei"))
         .agg(pl.col("full_name").first().alias("firds_name"), pl.len().alias("firds_n_shares"),
              pl.col("isin").str.slice(0, 2).first().alias("firds_isin_cc"), pl.col("isin").alias("firds_isins")))
    df = e.join(f, on="lei", how="full", coalesce=True)
    if parts.get("dila") is not None and parts["dila"].height:
        df = df.join(parts["dila"], on="lei", how="full", coalesce=True)
    if parts.get("afm") is not None and parts["afm"].height:
        df = df.join(parts["afm"].filter(pl.col("lei").is_not_null()).unique("lei"), on="lei", how="full", coalesce=True)
    if parts.get("fca") is not None and parts["fca"].height:
        df = df.join(parts["fca"], on="lei", how="full", coalesce=True)
    df = df.with_columns(
        pl.coalesce(pl.col("firds_isin_cc").replace_strict(EUROPE_ISIN, default=None), pl.col("esef_country")).alias("country"),
        pl.col("lei").is_in(gle.get_column("lei").unique().to_list()).alias("in_gleif_isin_map"))
    out = census_dir(s) / "filers.parquet"
    _write_parquet(df, out)
    eur = df.filter(pl.col("country").is_in(sorted(set(EUROPE_ISIN.values()))))
    summary = {
        "esef_filings": esef.height, "esef_leis": e.height,
        "esef_leis_europe": e.filter(pl.col("esef_country").is_in(sorted(set(EUROPE_ISIN.values())))).height,
        "esef_leis_fy2025": e.filter(pl.col("esef_latest_period") >= "2025-06-30").height,
        "firds_share_isins_europe": int(f.get_column("firds_n_shares").sum()), "firds_issuer_leis": f.height,
        "gleif_isin_lei_rows_europe": gle.height,
        "dila_leis": None if parts.get("dila") is None else parts["dila"].height,
        "afm_issuers": None if parts.get("afm") is None else parts["afm"].height,
        "fca_leis": None if parts.get("fca") is None else parts["fca"].height,
        "filers_rows": df.height, "filers_europe": eur.height,
        "per_issuer_archives": "emarket_it, nasdaq_nordic, oslo_newsweb, cnmv_es, fsma_be have no market-wide "
                               "filer listing in their adapters; they are queried per issuer for all eu_broad issuers "
                               "in step 5 (coverage_universe=eu_broad)",
        "runtime_s": round(time.time() - t0),
    }
    update_state("1_census", s, status="done", summary=summary, output=str(out))
    return summary


# ------------------------------------------------------------------------------------------------ step 2: identity


def _candidates(settings: Settings) -> pl.DataFrame:
    """FIRDS ordinary/common shares (CFI ES*) with a European ISIN; LEI from FIRDS, else the GLEIF ISIN-LEI file."""
    from app.normalize.identity_universe import fetch_firds_equities

    firds = fetch_firds_equities(settings)
    gle = gleif_isin_lei(settings)
    c = firds.filter(pl.col("cfi").str.starts_with("ES") & pl.col("isin").str.slice(0, 2).is_in(list(EUROPE_ISIN)))
    g = gle.unique("isin").rename({"lei": "gleif_lei"})
    c = c.join(g, on="isin", how="left").with_columns(
        pl.coalesce("issuer_lei", "gleif_lei").alias("lei"),
        pl.when(pl.col("gleif_lei").is_not_null() & (pl.col("gleif_lei") == pl.col("issuer_lei"))).then(pl.lit("gleif_isin"))
        .when(pl.col("issuer_lei").is_not_null()).then(pl.lit("esma_firds"))
        .when(pl.col("gleif_lei").is_not_null()).then(pl.lit("gleif_isin")).otherwise(None).alias("lei_source"),
        pl.col("isin").str.slice(0, 2).replace_strict(EUROPE_ISIN, default=None).alias("country"))
    return c


def step_identity(settings: Settings | None = None, chunk: int = 200) -> dict[str, Any]:
    from app.normalize.identity_figi import _job_key, map_jobs

    s = _s(settings)
    t0 = time.time()
    cand = _candidates(s)
    isins = cand.get_column("isin").to_list()
    jobs = [{"idType": "ID_ISIN", "idValue": i} for i in isins]
    st = load_state(s).get("steps", {}).get("2_identity", {})
    done = int(st.get("figi_chunks_done", 0))
    update_state("2_identity", s, status="running", n_candidates=len(isins), figi_chunk_size=chunk)
    res: dict[str, list[dict[str, Any]]] = {}
    for k in range(0, len(jobs), chunk):
        part = jobs[k:k + chunk]
        res.update(map_jobs(part, settings=s, max_age_days=60))  # cached: done chunks return instantly
        if k // chunk + 1 > done:
            done = k // chunk + 1
            update_state("2_identity", s, figi_chunks_done=done, figi_chunks_total=-(-len(jobs) // chunk))
            log.info("openfigi chunk %d/%d", done, -(-len(jobs) // chunk))
    missing = sum(1 for j in jobs if _job_key(j) not in res)
    # ---- home listing + Yahoo candidates
    rows = []
    for r in cand.iter_rows(named=True):
        recs = res.get(_job_key({"idType": "ID_ISIN", "idValue": r["isin"]}))
        best = pick_home_listing(recs or [], r["country"], r.get("venue_mic"))
        if not best:
            continue
        ex = best["_exch"]
        rows.append({"isin": r["isin"], "lei": r["lei"], "lei_source": r["lei_source"], "country": r["country"],
                     "cfi": r["cfi"], "firds_name": r["full_name"], "firds_venue": r.get("venue_mic"),
                     "figi": best.get("figi"), "composite_figi": best.get("compositeFIGI"),
                     "share_class_figi": best.get("shareClassFIGI"), "figi_name": best.get("name"),
                     "bbg_ticker": best.get("ticker"), "exch_code": ex, "mic": BBG_VENUE[ex][0],
                     "yf_candidates": bbg_to_yf_candidates(best.get("ticker") or "", ex)})
    lst = pl.DataFrame(rows, schema_overrides={"yf_candidates": pl.List(pl.Utf8)}) if rows else pl.DataFrame()
    update_state("2_identity", s, n_home_listing=lst.height, figi_missing=missing)
    # ---- price verification (yfinance batch download, resumable per batch)
    px = verify_prices(sorted({c for cs in lst.get_column("yf_candidates").to_list() for c in cs}), s)
    ok = {r["yf_ticker"]: r for r in px.filter(pl.col("n_obs") >= 10).iter_rows(named=True)}
    yf_pick = []
    for cs in lst.get_column("yf_candidates").to_list():
        yf_pick.append(next((c for c in cs if c in ok), None))
    lst = lst.with_columns(pl.Series("yf_ticker", yf_pick, dtype=pl.Utf8)).join(
        px.rename({"yf_ticker": "yf_ticker"}), on="yf_ticker", how="left")
    out = census_dir(s) / "listings.parquet"
    _write_parquet(lst, out)
    summary = {"candidates": len(isins), "figi_unmapped": missing, "home_listing": lst.height,
               "price_verified": lst.filter(pl.col("yf_ticker").is_not_null()).height,
               "by_country_verified": dict(lst.filter(pl.col("yf_ticker").is_not_null()).group_by("country").len()
                                           .sort("len", descending=True).iter_rows()),
               "runtime_s": round(time.time() - t0)}
    update_state("2_identity", s, status="done", summary=summary, output=str(out))
    return summary


def verify_prices(symbols: list[str], settings: Settings | None = None, batch: int = 150,
                  retries: int = 2) -> pl.DataFrame:
    """yfinance 3-month daily download per batch -> yf_ticker, n_obs, last_close, last_date, med_value_local
    (median close*volume of the last 60 sessions, quote units). Batches are persisted (resume skips done ones)."""
    from app.core.ratelimit import get_limiter

    s = _s(settings)
    cache_p = census_dir(s) / "price_check.parquet"
    have = pl.read_parquet(cache_p) if cache_p.exists() else pl.DataFrame(
        schema={"yf_ticker": pl.Utf8, "n_obs": pl.Int64, "last_close": pl.Float64, "last_date": pl.Date,
                "med_value_local": pl.Float64, "checked_at": pl.Datetime("us", "UTC")})
    done = set(have.get_column("yf_ticker").to_list())
    todo = [t for t in symbols if t not in done]
    run_start = datetime.now(UTC)
    import yfinance as yf

    lim = get_limiter("yfinance")
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        lim.acquire()
        rows = []
        try:
            data = yf.download(part, period="3mo", interval="1d", group_by="ticker", auto_adjust=False,
                               threads=True, progress=False)
        except Exception as e:  # noqa: BLE001
            log.warning("yf download batch failed: %s", e)
            time.sleep(30)
            continue
        now = datetime.now(UTC)
        for t in part:
            try:
                d = data[t] if len(part) > 1 else data
                d = d.dropna(subset=["Close"])
            except Exception:  # noqa: BLE001
                d = None
            if d is None or len(d) == 0:
                rows.append({"yf_ticker": t, "n_obs": 0, "last_close": None, "last_date": None,
                             "med_value_local": None, "checked_at": now})
                continue
            tail = d.tail(60)
            val = (tail["Close"] * tail["Volume"].fillna(0)).median()
            rows.append({"yf_ticker": t, "n_obs": int(len(d)), "last_close": float(d["Close"].iloc[-1]),
                         "last_date": d.index[-1].date(), "med_value_local": float(val) if val == val else None,
                         "checked_at": now})
        have = pl.concat([have, pl.DataFrame(rows, schema=have.schema)])
        _write_parquet(have, cache_p)
        log.info("price check %d/%d", min(i + batch, len(todo)), len(todo))
    # threaded batches silently drop symbols when Yahoo throttles (NWG.L, NOVO-B.CO came back empty): re-check the
    # empty ones sequentially in small batches, up to `retries` passes (each pass only once per run, resumable)
    for p in range(retries):
        last = have.unique("yf_ticker", keep="last")
        zeros = last.filter(pl.col("yf_ticker").is_in(symbols) & (pl.col("n_obs") == 0)
                            & (pl.col("checked_at") < run_start)).get_column("yf_ticker").to_list()
        if not zeros:
            break
        log.info("price re-check pass %d: %d empty symbols", p + 1, len(zeros))
        for i in range(0, len(zeros), 40):
            part = zeros[i:i + 40]
            data = None
            for attempt in range(8):  # Yahoo 429: back off (2, 4, 8 ... 30 min) instead of caching empties
                lim.acquire()
                try:
                    data = yf.download(part, period="3mo", interval="1d", group_by="ticker", auto_adjust=False,
                                       threads=False, progress=False)
                except Exception as e:  # noqa: BLE001
                    log.warning("yf re-check batch failed: %s", e)
                    data = None
                from yfinance import shared as _yfs
                errs = " ".join(str(v) for v in (getattr(_yfs, "_ERRORS", {}) or {}).values())
                if data is not None and "RateLimit" not in errs and "Too Many" not in errs:
                    break
                wait = min(1800, 120 * 2 ** attempt)
                log.warning("yahoo throttled (batch %d); sleeping %ds", i // 40, wait)
                update_state("2_identity", None, throttled_at=datetime.now(UTC).isoformat(timespec="seconds"))
                time.sleep(wait)
                data = None
            if data is None:
                continue
            have = pl.concat([have, pl.DataFrame(_bar_stats(data, part), schema=have.schema)])
            _write_parquet(have, cache_p)
            time.sleep(2)
            if i % 400 == 0:
                log.info("price re-check %d/%d", i + len(part), len(zeros))
        run_start = datetime.now(UTC)
    return have.filter(pl.col("yf_ticker").is_in(symbols)).unique("yf_ticker", keep="last").drop("checked_at")


def _bar_stats(data: Any, part: list[str]) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    rows = []
    for t in part:
        try:
            d = data[t] if len(part) > 1 else data
            d = d.dropna(subset=["Close"])
        except Exception:  # noqa: BLE001
            d = None
        if d is None or len(d) == 0:
            rows.append({"yf_ticker": t, "n_obs": 0, "last_close": None, "last_date": None,
                         "med_value_local": None, "checked_at": now})
            continue
        tail = d.tail(60)
        val = (tail["Close"] * tail["Volume"].fillna(0)).median()
        rows.append({"yf_ticker": t, "n_obs": int(len(d)), "last_close": float(d["Close"].iloc[-1]),
                     "last_date": d.index[-1].date(), "med_value_local": float(val) if val == val else None,
                     "checked_at": now})
    return rows


# ------------------------------------------------------------------------------------------------ step 3: universe


def _quote_ccy(mic: str) -> str:
    from app.normalize.identity_figi import mic_info

    return mic_info(mic).get("ccy") or {"XPRA": "CZK", "XBUD": "HUF", "XBSE": "RON", "XICE": "ISK"}.get(mic, "EUR")


def existing_keys(settings: Settings | None = None) -> tuple[set[str], set[str]]:
    """(curated-seed issuer keys, other reference keys) as 'isin:..' / 'lei:..' / 'yf:..' strings.

    Curated seeds (mvp_global) that pass the eu_broad rule are written to eu_broad.csv and flagged by ref_universe;
    rows matching only a non-seed reference issuer (e.g. a US-listed domestic SEC filer with an IE/CH/JE ISIN such as
    Linde or Accenture, already in mvp_us) are skipped so no issuer is created twice."""
    from app.core.storage import read_table
    from app.normalize.identity_universe import load_global_seeds

    s = _s(settings)
    seed_keys: set[str] = set()
    ref_keys: set[str] = set()
    pre = {"isin": "isin", "lei": "lei", "yf_ticker": "yf"}
    im = read_table("identifier_map", settings=s)
    seeded_iids: set[str] = set()
    p = s.data_dir / "reference" / "registry" / "issuer_extra.parquet"
    if p.exists():
        ex = pl.read_parquet(p)
        seeded_iids = set(ex.filter(pl.col("seeded").fill_null(False)).get_column("issuer_id").to_list())
    if im.height:
        im = im.filter(pl.col("valid_to").is_null() & pl.col("id_type").is_in(list(pre)))
        for r in im.select("id_type", "id_value", "issuer_id").iter_rows(named=True):
            k = f"{pre[r['id_type']]}:{r['id_value']}"
            (seed_keys if r["issuer_id"] in seeded_iids else ref_keys).add(k)
    seeds = load_global_seeds(s)
    for t, pf in pre.items():
        if t in seeds.columns:
            seed_keys |= {f"{pf}:{v}" for v in seeds.get_column(t).to_list() if v}
    return seed_keys, ref_keys - seed_keys


def _match_flags(r: dict[str, Any], seed_keys: set[str], ref_keys: set[str]) -> dict[str, bool]:
    ks = {f"isin:{r['isin']}", f"yf:{r['yf_ticker']}"} | ({f"lei:{r['lei']}"} if r.get("lei") else set())
    return {"seed_match": bool(ks & seed_keys), "ref_match": bool(ks & ref_keys)}


def step_universe(settings: Settings | None = None, min_mcap_eur: float = MIN_MCAP_EUR,
                  min_adv_eur: float = MIN_ADV_EUR) -> dict[str, Any]:
    from app.normalize.identity import _mcap_usd
    from app.normalize.identity_universe import fetch_fx_spot, fetch_yf_info, seeds_dir

    s = _s(settings)
    t0 = time.time()
    lst = pl.read_parquet(census_dir(s) / "listings.parquet").filter(pl.col("yf_ticker").is_not_null())
    lst = lst.with_columns(pl.col("mic").map_elements(_quote_ccy, return_dtype=pl.Utf8).alias("quote_ccy_guess"))
    fx = fetch_fx_spot(set(lst.get_column("quote_ccy_guess").to_list()) | {"EUR", "GBP"}, settings=s)
    fx_map = dict(zip(fx.get_column("currency").to_list(), fx.get_column("usd_per_unit").to_list(), strict=True))
    eur_usd = fx_map.get("EUR") or 1.08

    def to_eur(v: float | None, ccy: str | None) -> float | None:
        if v is None:
            return None
        c = {"GBp": "GBP", "GBX": "GBP"}.get(ccy or "EUR", ccy or "EUR")
        r = fx_map.get(c.upper())
        div = 100.0 if ccy in ("GBp", "GBX") else 1.0
        return v / div * r / eur_usd if r else None

    lst = lst.with_columns(pl.struct("med_value_local", "quote_ccy_guess").map_elements(
        lambda x: to_eur(x["med_value_local"], x["quote_ccy_guess"]), return_dtype=pl.Float64).alias("adv_eur_guess"))
    pre = lst.filter(pl.col("adv_eur_guess").fill_null(0) >= PREFILTER_ADV_EUR)
    update_state("3_universe", s, status="running", n_verified=lst.height, n_prefilter=pre.height)
    # yfinance .info (cached 30 d in registry/yf_info.parquet, checkpointed every 100 tickers -> resumable)
    tks = pre.get_column("yf_ticker").to_list()
    for attempt in range(8):  # Yahoo throttling: failed .info rows are retried with growing pauses
        yfi = fetch_yf_info(tks, settings=s, max_age_days=30, refetch_failed=True)
        n_bad = len(tks) - yfi.filter(pl.col("ok").fill_null(False)).height
        update_state("3_universe", s, yf_info_missing=n_bad, yf_info_attempt=attempt + 1)
        if n_bad <= max(10, len(tks) // 100):
            break
        wait = min(1800, 300 * 2 ** attempt)
        log.warning("yf info: %d/%d missing (attempt %d); sleeping %ds", n_bad, len(tks), attempt + 1, wait)
        time.sleep(wait)
    yf_by = {r["yf_ticker"]: r for r in yfi.iter_rows(named=True)}
    seed_keys, ref_keys = existing_keys(s)
    rows = []
    for r in pre.iter_rows(named=True):
        y = yf_by.get(r["yf_ticker"]) or {}
        mc_usd = _mcap_usd(y, fx_map) if y else None
        ccy = y.get("currency") or r["quote_ccy_guess"]
        adv = to_eur(r["med_value_local"], ccy)
        rows.append({**{k: r[k] for k in ("isin", "lei", "lei_source", "country", "mic", "yf_ticker", "bbg_ticker",
                                          "firds_name", "figi_name", "share_class_figi", "composite_figi", "cfi")},
                     "long_name": y.get("longName") or y.get("shortName"), "currency": ccy,
                     "mcap_eur": mc_usd / eur_usd if mc_usd else None, "adv_eur": adv,
                     **_match_flags(r, seed_keys, ref_keys)})
    u = pl.DataFrame(rows, infer_schema_length=None)
    elig = u.filter((pl.col("mcap_eur").fill_null(0) >= min_mcap_eur) & (pl.col("adv_eur").fill_null(0) >= min_adv_eur))
    # one security per issuer (LEI): the most liquid line
    elig = elig.sort("adv_eur", descending=True).unique(subset=["lei"], keep="first", maintain_order=True)
    # rows that are already a non-seed reference issuer (US domestic filers with a European ISIN) stay out
    skipped_ref = elig.filter(pl.col("ref_match") & ~pl.col("seed_match"))
    elig = elig.filter(~(pl.col("ref_match") & ~pl.col("seed_match")))
    _write_parquet(u, census_dir(s) / "universe_candidates.parquet")
    _write_parquet(elig, census_dir(s) / "eu_broad.parquet")
    seed_rows = []
    for r in elig.sort("country", "mcap_eur", descending=[False, True]).iter_rows(named=True):
        sym = r["yf_ticker"].rsplit(".", 1)[0]
        name = r["long_name"] or r["figi_name"] or r["firds_name"]
        seed_rows.append({"ticker": sym, "mic": r["mic"], "yf_ticker": r["yf_ticker"], "isin": r["isin"],
                          "name": name, "country": r["country"], "region": "UK" if r["country"] == "GB" else "EU",
                          "adr_ticker": None, "issuer_group": None, "share_class": None,
                          "notes": f"eu_broad mcap_eur={r['mcap_eur']:.0f} adv_eur={r['adv_eur']:.0f}",
                          "lei": r["lei"], "fye_mmdd": None, "lei_source": r["lei_source"]})
    seeds = pl.DataFrame(seed_rows, schema={k: pl.Utf8 for k in (
        "ticker", "mic", "yf_ticker", "isin", "name", "country", "region", "adr_ticker", "issuer_group", "share_class",
        "notes", "lei", "fye_mmdd", "lei_source")})
    seeds_p = seeds_dir(s) / "eu_broad.csv"
    tmp = seeds_p.with_suffix(".tmp")
    seeds.write_csv(tmp)
    tmp.replace(seeds_p)
    summary = {"verified_listings": lst.height, "prefiltered": pre.height, "with_mcap": u.filter(pl.col("mcap_eur").is_not_null()).height,
               "eu_broad": elig.height, "seed_rows": seeds.height,
               "matching_curated_seeds": elig.filter(pl.col("seed_match")).height,
               "skipped_existing_non_seed_issuer": skipped_ref.height,
               "skipped_examples": skipped_ref.sort("mcap_eur", descending=True).get_column("yf_ticker").head(15).to_list(),
               "by_country": dict(elig.group_by("country").len().sort("len", descending=True).iter_rows()),
               "runtime_s": round(time.time() - t0)}
    update_state("3_universe", s, status="seeds_written", summary=summary, seeds=str(seeds_p))
    # register: ref_universe (rebalance writes universe_membership; registry keeps issuer ids stable)
    if not load_state(s)["steps"]["3_universe"].get("ref_universe"):
        before = universe_counts(s)
        wait_idle()
        out = run_job("ref_universe", {"mode": "rebalance"}, s)
        after = universe_counts(s)
        summary.update({"ref_universe": out, "universe_before": before, "universe_after": after,
                        "mvp_global_unchanged": before.get("mvp_global") == after.get("mvp_global")})
        update_state("3_universe", s, ref_universe=out, summary=summary)
    update_state("3_universe", s, status="done")
    return summary


def universe_counts(settings: Settings | None = None) -> dict[str, int]:
    from app.core.storage import read_table

    um = read_table("universe_membership", settings=settings)
    if um.is_empty():
        return {}
    return dict(um.filter(pl.col("valid_to").is_null()).group_by("universe_id").len().iter_rows())


def eu_broad_frame(settings: Settings | None = None) -> pl.DataFrame:
    return pl.read_parquet(census_dir(settings) / "eu_broad.parquet")


# ------------------------------------------------------------------------------------------------ steps 4-6: jobs

#: national OAM adapter per issuer country (coverage_backfill SOURCES); GB -> fca_nsm job; DE/IE/AT/PL/... -> ESEF only
OAM_BY_COUNTRY = {"FR": "dila_fr", "NL": "afm_nl", "BE": "fsma_be", "IT": "emarket_it", "SE": "nasdaq_nordic",
                  "DK": "nasdaq_nordic", "FI": "nasdaq_nordic", "NO": "oslo_newsweb", "ES": "cnmv_es"}
#: jobs of the orchestrator's refresh chain: step 6 must not start while one of them is running
CHAIN_JOBS = ("fundamentals_normalize", "ratios_kpis", "valuation_daily", "scoring", "moat", "forecast", "tech_levels",
              "confluence_zones", "build_serving_db", "identity_resolve", "ref_universe", "prices_finalize")


def running_jobs() -> list[str]:
    from app.scheduler.runner import JobRunStore

    try:
        df = JobRunStore().recent(200, None, "running", None)
    except Exception:  # noqa: BLE001
        return []
    if df is None or df.is_empty():
        return []
    # stale 'running' rows (crashed workers) older than 12 h are ignored
    cut = datetime.now(UTC) - timedelta(hours=12)
    col = "started_at" if "started_at" in df.columns else None
    if col:
        df = df.filter(pl.col(col).cast(pl.Datetime("us", "UTC"), strict=False) >= cut)
    return df.get_column("job_id").unique().to_list()


def wait_idle(jobs: Iterable[str] = CHAIN_JOBS, poll_s: int = 60, max_wait_s: int = 6 * 3600) -> None:
    t0 = time.time()
    while True:
        busy = sorted(set(running_jobs()) & set(jobs))
        if not busy:
            return
        if time.time() - t0 > max_wait_s:
            raise TimeoutError(f"still running after {max_wait_s}s: {busy}")
        log.info("waiting for running jobs: %s", busy)
        time.sleep(poll_s)


def run_job(job: str, params: dict[str, Any] | None = None, settings: Settings | None = None) -> dict[str, Any]:
    """Run one registry job in-process (runner job locks, job_runs rows). Retries 'already_running' after a wait."""
    from app.scheduler.runner import JobRegistry, JobRunner, RunOptions

    s = _s(settings)
    for attempt in range(30):
        rep = JobRunner(JobRegistry.load(settings=s)).run_jobs(
            [job], RunOptions(params=dict(params or {}), force=True, trigger="backfill", retries=1))
        o = rep.outcomes.get(job)
        out = {"job": job, "status": getattr(o, "status", None), "reason": getattr(o, "reason", None),
               "rows": getattr(o, "rows_written", None), "run_ids": getattr(o, "run_ids", []),
               "dur_s": round(getattr(o, "duration_s", 0.0) or 0.0), "msg": (getattr(o, "message", "") or "")[:400]}
        if out["status"] == "skipped" and out["reason"] == "already_running":
            log.info("%s already running; retry in 120 s (%d)", job, attempt)
            time.sleep(120)
            continue
        log.info("job %s -> %s", job, json.dumps(out, default=str)[:600])
        return out
    return out


def run_chunked(step: str, label: str, job: str, items: list[str], chunk: int,
                make_params, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Run `job` over `items` in fixed chunks; state[step][label_done] is the resume cursor. A chunk that was in flight
    when the process died is re-run with retry=True (make_params may then add reparse=true)."""
    s = _s(settings)
    st = load_state(s).get("steps", {}).get(step, {})
    done = int(st.get(f"{label}_done", 0))
    inflight = st.get(f"{label}_inflight")
    total = -(-len(items) // chunk) if items else 0
    outs = []
    update_state(step, s, **{f"{label}_total": total, f"{label}_items": len(items)})
    for i in range(done, total):
        part = items[i * chunk:(i + 1) * chunk]
        update_state(step, s, **{f"{label}_inflight": i})
        t0 = time.time()
        out = run_job(job, make_params(part, retry=(inflight == i)), s)
        out["chunk"], out["wall_s"] = i, round(time.time() - t0)
        outs.append(out)
        update_state(step, s, **{f"{label}_done": i + 1, f"{label}_last": out})
        log.info("%s %s chunk %d/%d done (%ss)", step, label, i + 1, total, out["wall_s"])
    return outs


def step_prices(settings: Settings | None = None, chunk: int = 150) -> dict[str, Any]:
    s = _s(settings)
    t0 = time.time()
    eub = eu_broad_frame(s)
    new = sorted(eub.filter(~pl.col("seed_match")).get_column("yf_ticker").to_list())
    update_state("4_prices", s, status="running", n_symbols=len(new))
    outs = run_chunked("4_prices", "yf", "backfill_prices", new, chunk,
                       lambda part, retry: {"symbols": part, "polygon_days": 0, "skip_finalize": True,
                                            "yf_period": "max"}, s)
    st = load_state(s)["steps"]["4_prices"]
    if not st.get("finalized"):
        wait_idle()
        fin = run_job("prices_finalize", {}, s)
        update_state("4_prices", s, finalized=fin["status"] in ("success", "partial"), finalize=fin)
    summary = {"symbols": len(new), "chunks": len(outs), "runtime_s": round(time.time() - t0)}
    update_state("4_prices", s, status="done", summary=summary)
    return summary


def _official_annual_by_lei(leis: list[str], settings: Settings) -> pl.DataFrame:
    """lei -> n official annual filings, latest fiscal year (filings_index, official sources only)."""
    from app.core.storage import read_table

    fi = read_table("filings_index", settings=settings)
    if fi.is_empty():
        return pl.DataFrame(schema={"lei": pl.Utf8, "n_official": pl.UInt32, "latest_fy": pl.Int64})
    off = fi.filter(pl.col("source_entity_id").is_in(leis) & (pl.col("form_class") == "annual")
                    & ~pl.col("source_id").str.starts_with("yf"))
    pe = pl.col("period_end").cast(pl.Utf8).str.slice(0, 4).cast(pl.Int64, strict=False)
    return off.group_by(pl.col("source_entity_id").alias("lei")).agg(
        pl.len().alias("n_official"), pl.coalesce(pl.col("fiscal_year").cast(pl.Int64, strict=False), pe).max().alias("latest_fy"))


def step_fundamentals(settings: Settings | None = None) -> dict[str, Any]:
    s = _s(settings)
    t0 = time.time()
    eub = eu_broad_frame(s)
    leis = sorted({x for x in eub.get_column("lei").to_list() if x})
    cu = "mvp|eu_broad"
    update_state("5_fundamentals", s, status="running", n_leis=len(leis))
    # 5a ESEF (filings.xbrl.org pre-built xBRL-JSON) for every eu_broad LEI; full history (since 2000)
    run_chunked("5_fundamentals", "esef", "esef_lei_sweep", leis, 40,
                lambda part, retry: {"mode": "per_lei", "coverage_universe": cu, "entities": part,
                                     "since": "2000-01-01"}, s)
    # 5b national OAMs for issuers ESEF does not cover with FY>=2025 (Arelle conversion is the slow part)
    cov = _official_annual_by_lei(leis, s)
    have = {r["lei"] for r in cov.filter(pl.col("latest_fy") >= 2025).iter_rows(named=True)}
    gaps = eub.filter(pl.col("lei").is_not_null() & ~pl.col("lei").is_in(list(have)))
    oam = gaps.filter(pl.col("country").is_in(list(OAM_BY_COUNTRY)))
    for src in sorted(set(OAM_BY_COUNTRY.values())):
        ctry = [c for c, v in OAM_BY_COUNTRY.items() if v == src]
        part_leis = sorted(oam.filter(pl.col("country").is_in(ctry)).get_column("lei").to_list())
        run_chunked("5_fundamentals", f"oam_{src}", "oam_filings_history", part_leis, 25,
                    lambda part, retry, src=src: {"sources": src, "entities": part, "since": "2020-01-01",
                                                  "coverage_universe": cu, "history": False, "max_new": 400,
                                                  **({"reparse": True} if retry else {})}, s)
    uk = sorted(gaps.filter(pl.col("country") == "GB").get_column("lei").to_list())
    run_chunked("5_fundamentals", "fca", "fca_nsm", uk, 40,
                lambda part, retry: {"entities": part, "include_dsh": False, "max_docs": 1000}, s)
    # 5c yfinance fallback for eu_broad names still without an official FY>=2025 annual report
    cov = _official_annual_by_lei(leis, s)
    have = {r["lei"] for r in cov.filter(pl.col("latest_fy") >= 2025).iter_rows(named=True)}
    yf_only = sorted(eub.filter(~pl.col("lei").is_in(list(have))).get_column("yf_ticker").to_list())
    run_chunked("5_fundamentals", "yf", "yf_fundamentals", yf_only, 100,
                lambda part, retry: {"entities": part}, s)
    _write_parquet(cov, census_dir(s) / "official_coverage.parquet")
    summary = {"leis": len(leis), "official_fy2025": len(have), "oam_targets": oam.height, "uk_targets": len(uk),
               "yf_fallback": len(yf_only), "runtime_s": round(time.time() - t0)}
    update_state("5_fundamentals", s, status="done", summary=summary)
    return summary


ANALYTICS_CHAIN: list[tuple[str, dict[str, Any]]] = [
    ("identity_resolve", {}), ("fundamentals_normalize", {"universe": "full", "full": True}), ("ratios_kpis", {}),
    ("valuation_daily", {}), ("scoring", {"mode": "history"}), ("scoring", {}), ("moat", {}), ("forecast", {}),
    ("tech_levels", {}), ("confluence_zones", {}), ("build_serving_db", {}),
]


def step_analytics(settings: Settings | None = None) -> dict[str, Any]:
    s = _s(settings)
    t0 = time.time()
    st = load_state(s).get("steps", {}).get("6_analytics", {})
    done = int(st.get("chain_done", 0))
    update_state("6_analytics", s, status="running")
    outs = list(st.get("outcomes") or [])
    for i in range(done, len(ANALYTICS_CHAIN)):
        job, params = ANALYTICS_CHAIN[i]
        wait_idle()
        out = run_job(job, params, s)
        outs.append(out)
        update_state("6_analytics", s, chain_done=i + 1, outcomes=outs)
    summary = {"jobs": [(o["job"], o["status"], o.get("dur_s")) for o in outs], "runtime_s": round(time.time() - t0)}
    update_state("6_analytics", s, status="done", summary=summary)
    return summary


# ------------------------------------------------------------------------------------------------ CLI


def _status(settings: Settings | None = None) -> dict[str, Any]:
    st = load_state(settings)
    st["running"] = running_steps(settings)
    return st


STEPS = {"census": step_census, "identity": step_identity, "universe": step_universe, "prices": step_prices,
         "fundamentals": step_fundamentals, "analytics": step_analytics}
STEP_KEYS = {"census": "1_census", "identity": "2_identity", "universe": "3_universe", "prices": "4_prices",
             "fundamentals": "5_fundamentals", "analytics": "6_analytics"}


def main(argv: Iterable[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    step = args[0] if args else "status"
    if step == "status":
        print(json.dumps(_status(), indent=2, default=str))
        return 0
    if step not in STEPS:
        print(f"unknown step {step}; one of {', '.join(STEPS)} | status")
        return 2
    s = get_settings()
    ld = log_dir(s)
    pid_p = ld / f"{step}.pid"
    if pid_p.exists():
        try:
            other = int(pid_p.read_text().strip())
        except ValueError:
            other = 0
        if other != os.getpid() and pid_alive(other):
            print(f"{step} already running (pid {other})")
            return 3
    pid_p.write_text(str(os.getpid()))
    fh = logging.FileHandler(ld / f"{step}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(fh)
    root.setLevel(logging.INFO)
    log.info("=== %s start (pid %d)", step, os.getpid())
    try:
        out = STEPS[step](s)
        log.info("=== %s done: %s", step, json.dumps(out, default=str)[:2000])
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("=== %s failed", step)
        update_state(STEP_KEYS[step], s, last_error=f"{type(e).__name__}: {str(e)[:300]}", status="error_resumable")
        return 1
    finally:
        try:
            pid_p.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
