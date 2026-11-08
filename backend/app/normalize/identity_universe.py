"""Reference-universe sources and universe selection. Owner: ref-identity.

Jobs (contracts/jobs.yaml):
    ref_universe       (nightly)  run(ctx)                    -> rebuild issuers / securities_master / listings / identifier_map
    universe_rebalance (monthly)  run(ctx, params.mode=rebalance) -> also rewrites universe_membership

Sources (each fetcher lands the raw payload under data/raw/<source_id>/ref_<dataset>/dt=YYYY-MM-DD/ and returns a
Polars frame; every fetcher is fault-tolerant and returns an empty frame on failure so one dead source never
blocks the build):

    SEC        company_tickers_exchange.json, submissions/CIK*.json (cached 30 d), frames dei shares outstanding
    Massive    /v3/reference/tickers (type, primary MIC, FIGIs, CIK), grouped-daily close/volume (1 call)
    yfinance   .info (sector/industry/marketCap/financialCurrency/website/employees; cached 30 d), FX spot
    JPX        data_j.xlsx (code, TSE33)            EDINET  Edinetcode.zip (edinet code <-> sec code, EN name)
    OpenDART   corpCode.xml (corp_code <-> stock_code), company.json (KSIC induty_code, cached)
    KRX KIND   corpList (industry name, fiscal month)
    SSE/SZSE   A-share lists (CSRC industry)        HKEX    ListOfSecurities.xlsx (ISIN, board lot)
    CVM        cad_cia_aberta.csv (CD_CVM, CNPJ, sector), FCA valor_mobiliario (CNPJ <-> B3 ticker)
    B3         COTAHIST daily (ticker <-> ISIN)
    ESMA FIRDS FULINS_E (ISIN <-> issuer LEI <-> MIC <-> CFI; cached 7 d)   GLEIF  lei-records (names, country, regno)

Universe rules (UNIVERSE_RULES): MVP S1 = mvp_us (top 500 US-listed domestic common stocks by market cap) +
mvp_global (curated seeds/mvp_global.csv). `full` = scale-up S2 definition (US mcap >= $300M & price >= $5,
plus every global seed). S3 (all parseable) is documented, not materialized.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import time
import zipfile
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient, HttpError
from app.core.logsetup import get_logger
from app.core.ratelimit import RateLimit, get_limiter

log = get_logger(__name__)

UNIVERSE_RULES: dict[str, dict[str, Any]] = {
    "mvp_us": {
        "stage": "S1", "size": 500, "rank_by": "market_cap_usd",
        "include": "US-listed (XNAS/XNYS/XASE/BATS) common stock (Massive type CS), SEC 10-K filer (domestic), "
                   "issuer not linked to a non-US home listing",
        "exclude": "ADRs, ETFs/funds, preferreds/warrants/units/rights, SPACs (SIC 6770), 20-F/40-F filers, "
                   "investment trusts (SIC 6221/6722/6726/6798 only if not REIT common), shell companies",
        "one_security_per_issuer": True,
    },
    "mvp_global": {"stage": "S1", "source": "data/reference/seeds/mvp_global.csv", "size": "~230 curated",
                   "rank_by": "market_cap_usd within region"},
    "eu_broad": {"stage": "S2", "source": "data/reference/seeds/eu_broad.csv (app.ingestion.eu_expansion)",
                 "rule": "EEA/UK/CH/NO ordinary shares (FIRDS CFI ES*) on a primary venue, mcap >= EUR 300M (yfinance) "
                         "and median daily traded value >= EUR 50k; one security per LEI; curated mvp_global seeds "
                         "that pass are members too", "rank_by": "market_cap_usd within eu_broad"},
    "full": {"stage": "S2", "us": {"min_mcap_usd": 300e6, "min_price": 5.0}, "global": "all seeds + resolved listings",
             "notes": "S3 = everything with prices + fundamentals (~15-20k); add national lists (JPX/KRX/SSE/SZSE/HKEX/"
                      "CVM/FIRDS) as listing sources and rank with yfinance marketCap"},
}

US_EXCHANGES = ("XNAS", "XNYS", "XASE", "BATS", "XTXS", "ARCX")
EXCLUDED_SIC = {"6770", "6221", "6722", "6726"}


# ------------------------------------------------------------------------------------------------ helpers


def _today() -> date:
    return datetime.now(UTC).date()


def registry_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    p = s.data_dir / "reference" / "registry"
    p.mkdir(parents=True, exist_ok=True)
    return p


def seeds_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return s.data_dir / "reference" / "seeds"


def land_raw(source_id: str, dataset: str, name: str, content: bytes, settings: Settings | None = None,
             compress: bool = True) -> Path:
    """Write a raw payload to data/raw/<source_id>/ref_<dataset>/dt=<today>/<name>[.gz] (atomic)."""
    s = settings or get_settings()
    d = s.raw_dir / source_id / f"ref_{dataset}" / f"dt={_today().isoformat()}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / (name + (".gz" if compress else ""))
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(gzip.compress(content, 5) if compress else content)
    tmp.replace(p)
    return p


def _cache_read(name: str, settings: Settings | None = None) -> pl.DataFrame | None:
    p = registry_dir(settings) / f"{name}.parquet"
    return pl.read_parquet(p) if p.exists() else None


def _cache_write(df: pl.DataFrame, name: str, settings: Settings | None = None) -> None:
    p = registry_dir(settings) / f"{name}.parquet"
    tmp = p.with_suffix(".tmp")
    df.write_parquet(tmp)
    tmp.replace(p)


def _safe(fn):
    """Decorator: log and return an empty DataFrame instead of raising (one dead source must not block the build)."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            log.warning("reference source %s failed: %s: %s", fn.__name__, type(e).__name__, str(e)[:300])
            return pl.DataFrame()
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/128.0 Safari/537.36")


def _client(source_id: str, settings: Settings | None = None, **kw: Any) -> HttpClient:
    return HttpClient(source_id, settings=settings, **kw)


def _str(x: Any) -> str | None:
    if x is None:
        return None
    s = str(x).strip()
    return s or None


# ------------------------------------------------------------------------------------------------ seeds


SEED_COLUMNS = ["ticker", "mic", "yf_ticker", "isin", "name", "country", "region", "adr_ticker", "issuer_group",
                "share_class", "notes", "lei", "fye_mmdd"]


def load_global_seeds(settings: Settings | None = None) -> pl.DataFrame:
    """Curated seeds/mvp_global.csv (ticker, mic, yf_ticker, isin, name, country, region, adr_ticker, issuer_group,
    share_class, notes). All columns are strings; codes keep leading zeros."""
    p = seeds_dir(settings) / "mvp_global.csv"
    if not p.exists():
        return pl.DataFrame(schema={c: pl.Utf8 for c in SEED_COLUMNS})
    df = pl.read_csv(p, infer_schema_length=0, encoding="utf8")
    for c in SEED_COLUMNS:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias(c))
    return df.select(SEED_COLUMNS).with_columns(
        [pl.col(c).str.strip_chars().replace("", None) for c in SEED_COLUMNS]
    ).filter(pl.col("ticker").is_not_null() & pl.col("mic").is_not_null())


def load_eu_broad_seeds(settings: Settings | None = None) -> pl.DataFrame:
    """Generated seeds/eu_broad.csv (app.ingestion.eu_expansion step 3): SEED_COLUMNS + lei_source. Empty if absent."""
    cols = [*SEED_COLUMNS, "lei_source"]
    p = seeds_dir(settings) / "eu_broad.csv"
    if not p.exists():
        return pl.DataFrame(schema={c: pl.Utf8 for c in cols})
    df = pl.read_csv(p, infer_schema_length=0, encoding="utf8")
    for c in cols:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias(c))
    return df.select(cols).with_columns([pl.col(c).str.strip_chars().replace("", None) for c in cols]).filter(
        pl.col("ticker").is_not_null() & pl.col("mic").is_not_null())


COVERAGE_COLUMNS = [*SEED_COLUMNS, "native_industry", "coverage_source"]


def load_coverage_seeds(settings: Settings | None = None) -> pl.DataFrame:
    """Generated seeds/price_coverage.csv (app.ingestion.price_coverage_ext `seeds`): every listed ordinary share of the
    markets with a bulk price source (CN BaoStock, TW TWSE/TPEx, BR COTAHIST; P2: JP/KR/HK lists). SEED_COLUMNS +
    native_industry (CSRC / TWSE industry code) + coverage_source. Members of `full` only. Empty if absent."""
    frames = []
    # coverage v2 W2: seeds/regulatory_register.csv (app.ingestion.regulatory_register) = EU / UK register issuers that
    # were not in the reference data (ESMA / FCA FIRDS ISIN -> OpenFIGI home listing -> verified Yahoo symbol); same
    # columns, coverage_source 'regulatory_register:<register>'. Read after price_coverage.csv (its rows win).
    for name in ("price_coverage.csv", "regulatory_register.csv"):
        p = seeds_dir(settings) / name
        if not p.exists():
            continue
        df = pl.read_csv(p, infer_schema_length=0, encoding="utf8")
        for c in COVERAGE_COLUMNS:
            if c not in df.columns:
                df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias(c))
        frames.append(df.select(COVERAGE_COLUMNS))
    if not frames:
        return pl.DataFrame(schema={c: pl.Utf8 for c in COVERAGE_COLUMNS})
    df = pl.concat(frames, how="vertical")
    return df.with_columns([pl.col(c).str.strip_chars().replace("", None)
                            for c in COVERAGE_COLUMNS]).filter(
        pl.col("ticker").is_not_null() & pl.col("mic").is_not_null())


def load_adr_ratios(settings: Settings | None = None) -> dict[str, float]:
    """seeds/adr_ratios.csv -> {normalized ADR ticker: underlying ordinary shares per ADS}.

    Columns: adr_ticker, adr_ratio, underlying, verified (price | public | unverified | sec_cover), note. Tickers are
    matched upper-case with '.' / '-' / '/' removed ('PBR-A' == 'PBR.A'). Coverage v3 A6: seeds/adr_ratios_sec.csv
    (generated from SEC 20-F / 40-F cover pages by app.ingestion.adr_ratios_sec) fills the tickers the curated file
    does not list; a curated entry always wins."""
    out: dict[str, float] = {}
    for name in ("adr_ratios_sec.csv", "adr_ratios.csv"):  # curated last: it overrides the generated values
        p = seeds_dir(settings) / name
        if not p.exists():
            continue
        try:
            df = pl.read_csv(p, infer_schema_length=0, encoding="utf8", comment_prefix="#")
        except Exception:  # noqa: BLE001 - a broken generated file must not block ref_universe
            log.warning("adr ratio seed %s unreadable", name)
            continue
        for r in df.iter_rows(named=True):
            tk, v = (r.get("adr_ticker") or "").strip(), (r.get("adr_ratio") or "").strip()
            try:
                ratio = float(v)
            except ValueError:
                continue
            if tk and ratio > 0:
                out[adr_ticker_key(tk)] = ratio
    return out


def adr_ticker_key(t: str | None) -> str:
    return re.sub(r"[.\-/ ]", "", (t or "").upper())


def read_seed_csv(p: Path) -> pl.DataFrame:
    """Hand-edited seed CSVs: a note containing an unquoted comma used to break ref_universe (F17: 'found more fields
    than defined in Schema'). Ragged lines are read with the extra fields folded into the last column."""
    try:
        return pl.read_csv(p, infer_schema_length=0, comment_prefix="#")
    except pl.exceptions.ComputeError:
        import csv

        log.warning("seed %s has ragged lines: extra fields are merged into the last column", p.name)
        with p.open(encoding="utf-8", newline="") as f:
            rows = [r for r in csv.reader(f) if r and not r[0].lstrip().startswith("#")]
        head, body = rows[0], rows[1:]
        n = len(head)
        fixed = [(r[: n - 1] + [",".join(r[n - 1:])]) if len(r) > n else (r + [""] * (n - len(r))) for r in body]
        return pl.DataFrame(fixed, schema={h: pl.Utf8 for h in head}, orient="row")


def load_overrides(settings: Settings | None = None) -> pl.DataFrame:
    """seeds/manual_overrides.csv: kind,key,value,note.

    kind: link (key and value are the same issuer, e.g. 'cik:0001119639','seed:petrobras'),
          block (never link key and value), subsector (key -> subsector id), name / country (issuer field),
          confirm (fuzzy mapping key -> issuer key is confirmed, confidence 1.0), exclude_universe (key).
    key/value forms: cik:..., lei:..., isin:..., ticker:SYM@MIC, seed:<issuer_group or SYM@MIC>, edinet:..., ...
    """
    p = seeds_dir(settings) / "manual_overrides.csv"
    if not p.exists():
        return pl.DataFrame(schema={"kind": pl.Utf8, "key": pl.Utf8, "value": pl.Utf8, "note": pl.Utf8})
    df = read_seed_csv(p)
    return df.with_columns([pl.col(c).str.strip_chars() for c in df.columns])


def load_successions(settings: Settings | None = None) -> pl.DataFrame:
    """seeds/issuer_successions.csv: predecessor,successor,effective_date,reason,note.

    Corporate re-domiciliations / holding-company reorganizations / CIK changes: both keys (e.g. 'cik:0000034088' ->
    'cik:0002115436') identify ONE issuer_id, so fundamentals and filings join across the CIK change. Auto-detected
    successions (app.normalize.identity.detect_successions) are appended to registry/successions_detected.csv."""
    p = seeds_dir(settings) / "issuer_successions.csv"
    schema = {"predecessor": pl.Utf8, "successor": pl.Utf8, "effective_date": pl.Utf8, "reason": pl.Utf8, "note": pl.Utf8}
    if not p.exists():
        return pl.DataFrame(schema=schema)
    df = read_seed_csv(p)
    return df.with_columns([pl.col(c).str.strip_chars() for c in df.columns])


def load_us_pins(settings: Settings | None = None) -> pl.DataFrame:
    """seeds/mvp_us.csv: ticker,action(include|exclude),note — manual pins applied on top of the mvp_us rule."""
    p = seeds_dir(settings) / "mvp_us.csv"
    if not p.exists():
        return pl.DataFrame(schema={"ticker": pl.Utf8, "action": pl.Utf8, "note": pl.Utf8})
    return pl.read_csv(p, infer_schema_length=0, comment_prefix="#")


# ------------------------------------------------------------------------------------------------ SEC


@_safe
def fetch_sec_tickers(settings: Settings | None = None, http: HttpClient | None = None) -> pl.DataFrame:
    """SEC company_tickers_exchange.json -> cik (10-digit), name, ticker (SEC '-' class format), exchange, mic."""
    from app.normalize.identity_figi import SEC_EXCHANGE_MIC

    h = http or _client("sec_edgar", settings)
    r = h.get("https://www.sec.gov/files/company_tickers_exchange.json", cache_ttl=6 * 3600)
    land_raw("sec_edgar", "company_tickers_exchange", "company_tickers_exchange.json", r.content, settings)
    d = r.json()
    rows = [dict(zip(d["fields"], x, strict=False)) for x in d["data"]]
    df = pl.DataFrame(rows, schema={"cik": pl.Int64, "name": pl.Utf8, "ticker": pl.Utf8, "exchange": pl.Utf8})
    return df.with_row_index("sec_rank").with_columns(
        pl.col("cik").cast(pl.Utf8).str.zfill(10),
        pl.col("exchange").replace_strict(SEC_EXCHANGE_MIC, default=None).alias("mic"),
        pl.col("sec_rank").cast(pl.Int32),
    )


_SUB_SCHEMA = {
    "cik": pl.Utf8, "sub_name": pl.Utf8, "entity_type": pl.Utf8, "sic": pl.Utf8, "sic_desc": pl.Utf8,
    "state_inc": pl.Utf8, "state_inc_desc": pl.Utf8, "biz_country": pl.Utf8, "biz_state": pl.Utf8,
    "biz_foreign": pl.Boolean, "fye_mmdd": pl.Utf8, "lei": pl.Utf8, "ein": pl.Utf8, "website": pl.Utf8,
    "description": pl.Utf8, "category": pl.Utf8, "latest_annual_form": pl.Utf8, "former_names": pl.List(pl.Utf8),
    "tickers": pl.List(pl.Utf8), "exchanges": pl.List(pl.Utf8), "succession_form": pl.Boolean,
    "last_filing_date": pl.Utf8, "fetched_at": pl.Datetime("us", "UTC"),
}
#: forms filed by a successor issuer under Rule 12g-3 (holding-company reorganizations / re-domiciliations)
SUCCESSION_FORMS = ("8-K12B", "8-K12B/A", "8-K12G3", "8-K12G3/A")

ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT")


def parse_submissions(j: dict[str, Any]) -> dict[str, Any]:
    """Extract identity metadata from one SEC submissions JSON."""
    biz = (j.get("addresses") or {}).get("business") or {}
    recent = (j.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    annual = next((f for f in forms if f in ANNUAL_FORMS), None)
    if annual is None and any(f in ("10-Q", "10-Q/A") for f in forms):
        annual = "10-K"
    fye = _str(j.get("fiscalYearEnd"))
    foreign = bool(biz.get("isForeignLocation")) if biz.get("isForeignLocation") is not None else None
    country = _str(biz.get("country")) if foreign else ("United States" if foreign is False else None)
    return {
        "cik": str(j.get("cik", "")).zfill(10),
        "sub_name": _str(j.get("name")),
        "entity_type": _str(j.get("entityType")),
        "sic": _str(j.get("sic")),
        "sic_desc": _str(j.get("sicDescription")),
        "state_inc": _str(j.get("stateOfIncorporation")),
        "state_inc_desc": _str(j.get("stateOfIncorporationDescription")),
        "biz_country": country,
        "biz_state": _str(biz.get("stateOrCountry")),
        "biz_foreign": foreign,
        "fye_mmdd": f"{fye[:2]}-{fye[2:]}" if fye and len(fye) == 4 else None,
        "lei": _str(j.get("lei")),
        "ein": _str(j.get("ein")),
        "website": _str(j.get("website")) or _str(j.get("investorWebsite")),
        "description": _str(j.get("description")),
        "category": _str(j.get("category")),
        "latest_annual_form": annual,
        "former_names": [x.get("name") for x in (j.get("formerNames") or []) if x.get("name")],
        "tickers": [t for t in (j.get("tickers") or []) if t],
        "exchanges": [e or "" for e in (j.get("exchanges") or [])],
        "succession_form": any(f in SUCCESSION_FORMS for f in forms),
        "last_filing_date": (recent.get("filingDate") or [None])[0],
    }


def fetch_sec_submissions(ciks: Iterable[str], settings: Settings | None = None, http: HttpClient | None = None,
                          max_age_days: int = 30, budget: int | None = None) -> pl.DataFrame:
    """Per-CIK submissions metadata (cached in registry/sec_submissions.parquet for max_age_days)."""
    s = settings or get_settings()
    cache = _cache_read("sec_submissions", s)
    if cache is None:
        cache = pl.DataFrame(schema=_SUB_SCHEMA)
    for c, t in _SUB_SCHEMA.items():  # older cache files: add new columns (rows re-parsed from the HTTP cache)
        if c not in cache.columns:
            cache = cache.with_columns(pl.lit(None, dtype=t).alias(c))
    cache = cache.select(list(_SUB_SCHEMA)).cast(_SUB_SCHEMA)  # type: ignore[arg-type]
    want = list(dict.fromkeys(str(c).zfill(10) for c in ciks if c))
    fresh = cache.filter((pl.col("fetched_at") >= datetime.now(UTC) - timedelta(days=max_age_days))
                         & pl.col("succession_form").is_not_null())
    have = set(fresh.get_column("cik").to_list())
    todo = [c for c in want if c not in have]
    if budget is not None:
        todo = todo[:budget]
    rows: list[dict[str, Any]] = []
    if todo:
        h = http or _client("sec_edgar", s)
        now = datetime.now(UTC)
        for i, cik in enumerate(todo):
            try:
                r = h.get(f"https://data.sec.gov/submissions/CIK{cik}.json", cache_ttl=max_age_days * 86400)
                rec = parse_submissions(r.json())
                rec["fetched_at"] = now
                rows.append(rec)
            except HttpError as e:
                log.debug("submissions %s: HTTP %s", cik, e.status)
            except Exception as e:  # noqa: BLE001
                log.warning("submissions %s failed: %s", cik, e)
            if i and i % 250 == 0:
                log.info("sec submissions: %d/%d", i, len(todo))
                _cache_write(pl.concat([cache, pl.DataFrame(rows, schema=_SUB_SCHEMA)]).unique(
                    subset=["cik"], keep="last"), "sec_submissions", s)
        if rows:
            cache = pl.concat([cache, pl.DataFrame(rows, schema=_SUB_SCHEMA)]).unique(subset=["cik"], keep="last")
            _cache_write(cache, "sec_submissions", s)
    return cache.filter(pl.col("cik").is_in(want))


@_safe
def fetch_sec_shares(settings: Settings | None = None, http: HttpClient | None = None,
                     as_of: date | None = None) -> pl.DataFrame:
    """Latest shares outstanding per CIK from SEC frames: dei EntityCommonStockSharesOutstanding (instant, last
    4 quarters), fallback us-gaap WeightedAverageNumberOfDilutedSharesOutstanding (quarterly). Columns: cik, shares,
    shares_date, shares_source, loc (e.g. 'US-CA')."""
    h = http or _client("sec_edgar", settings)
    d = as_of or _today()
    q = (d.month - 1) // 3 + 1
    periods = []
    y, qq = d.year, q
    for _ in range(5):
        periods.append((y, qq))
        qq -= 1
        if qq == 0:
            y, qq = y - 1, 4
    frames = []
    for concept, unit, inst, src in (
        ("dei/EntityCommonStockSharesOutstanding", "shares", "I", "dei"),
        ("us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", "shares", "", "us-gaap-wad"),
        ("us-gaap/WeightedAverageNumberOfSharesOutstandingBasic", "shares", "", "us-gaap-wab"),
    ):
        for (yy, qn) in periods:
            url = f"https://data.sec.gov/api/xbrl/frames/{concept}/{unit}/CY{yy}Q{qn}{inst}.json"
            try:
                r = h.get(url, cache_ttl=86400)
            except HttpError as e:
                if e.status != 404:
                    log.warning("frames %s: HTTP %s", url, e.status)
                continue
            data = r.json().get("data") or []
            if not data:
                continue
            frames.append(pl.DataFrame({
                "cik": [str(x["cik"]).zfill(10) for x in data],
                "shares": [float(x["val"]) for x in data],
                "shares_date": [x["end"] for x in data],
                "loc": [x.get("loc") for x in data],
                "entity_name": [x.get("entityName") for x in data],
            }).with_columns(pl.lit(src).alias("shares_source"), pl.col("shares_date").str.to_date()))
    if not frames:
        return pl.DataFrame()
    df = pl.concat(frames)
    prio = {"dei": 0, "us-gaap-wad": 1, "us-gaap-wab": 2}
    df = df.with_columns(pl.col("shares_source").replace_strict(prio, default=9).alias("_p"))
    # latest date wins; within ~45 days prefer dei
    df = df.sort(["cik", "shares_date", "_p"], descending=[False, True, False])
    latest = df.group_by("cik").agg(pl.col("shares_date").max().alias("_maxd"))
    df = df.join(latest, on="cik").filter(pl.col("shares_date") >= pl.col("_maxd") - pl.duration(days=120))
    return df.sort(["cik", "_p", "shares_date"], descending=[False, False, True]).unique(
        subset=["cik"], keep="first", maintain_order=True).drop(["_p", "_maxd"])


# ------------------------------------------------------------------------------------------------ Massive / Polygon


def _polygon_client(settings: Settings | None = None) -> HttpClient | None:
    s = settings or get_settings()
    key = s.secret("polygon_apikey")
    if not key:
        return None
    return HttpClient("polygon", base_url="https://api.massive.com", headers={"Authorization": f"Bearer {key}"},
                      settings=s)


@_safe
def fetch_polygon_tickers(settings: Settings | None = None, max_pages: int = 20) -> pl.DataFrame:
    """Massive /v3/reference/tickers (active US stocks): ticker (BRK.B format), name, primary_exchange, type,
    cik, composite_figi, share_class_figi, currency_name. ~13 pages (1 call each, 4/min budget)."""
    h = _polygon_client(settings)
    if h is None:
        log.info("polygon key missing: skip reference tickers")
        return pl.DataFrame()
    out: list[dict[str, Any]] = []
    url = "/v3/reference/tickers"
    params: dict[str, Any] | None = {"market": "stocks", "active": "true", "limit": 1000}
    pages = []
    for _ in range(max_pages):
        r = h.get(url, params=params, cache_ttl=20 * 3600)
        j = r.json()
        pages.append(j.get("results") or [])
        out.extend(j.get("results") or [])
        nxt = j.get("next_url")
        if not nxt:
            break
        url, params = nxt, None
    land_raw("polygon", "reference_tickers", "tickers.json", json.dumps(out).encode(), settings)
    cols = ["ticker", "name", "primary_exchange", "type", "cik", "composite_figi", "share_class_figi", "currency_name"]
    return pl.DataFrame([{c: x.get(c) for c in cols} for x in out], schema={c: pl.Utf8 for c in cols})


@_safe
def fetch_polygon_grouped(day: date | None = None, settings: Settings | None = None, lookback: int = 6) -> pl.DataFrame:
    """Whole-US-market daily bars for the latest trading day <= day (1 call per tried date)."""
    h = _polygon_client(settings)
    if h is None:
        return pl.DataFrame()
    d = day or (_today() - timedelta(days=1))
    for _ in range(lookback):
        if d.weekday() < 5:
            r = h.get(f"/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}", params={"adjusted": "true"},
                      cache_ttl=-1 if d < _today() - timedelta(days=1) else 3600)
            res = r.json().get("results") or []
            if res:
                land_raw("polygon", "grouped_daily", f"{d.isoformat()}.json", r.content, settings)
                return pl.DataFrame({
                    "ticker": [x["T"] for x in res], "close": [float(x.get("c") or 0) for x in res],
                    "volume": [float(x.get("v") or 0) for x in res],
                    "vwap": [float(x.get("vw") or x.get("c") or 0) for x in res],
                }).with_columns(pl.lit(d).alias("price_date"))
        d -= timedelta(days=1)
    return pl.DataFrame()


# ------------------------------------------------------------------------------------------------ yfinance

_YF_INFO_KEYS = ("longName", "shortName", "sector", "industry", "marketCap", "currency", "financialCurrency", "country",
                 "website", "fullTimeEmployees", "longBusinessSummary", "quoteType", "exchange", "regularMarketPrice",
                 "previousClose", "sharesOutstanding", "isin")
_YF_SCHEMA = {
    "yf_ticker": pl.Utf8, "longName": pl.Utf8, "shortName": pl.Utf8, "sector": pl.Utf8, "industry": pl.Utf8,
    "marketCap": pl.Float64, "currency": pl.Utf8, "financialCurrency": pl.Utf8, "country": pl.Utf8,
    "website": pl.Utf8, "fullTimeEmployees": pl.Int64, "longBusinessSummary": pl.Utf8, "quoteType": pl.Utf8,
    "exchange": pl.Utf8, "regularMarketPrice": pl.Float64, "previousClose": pl.Float64,
    "sharesOutstanding": pl.Float64, "isin": pl.Utf8, "ok": pl.Boolean, "fetched_at": pl.Datetime("us", "UTC"),
}


def fetch_yf_info(tickers: Sequence[str], settings: Settings | None = None, max_age_days: int = 30,
                  budget: int | None = None, pause_every: int = 100, pause_s: float = 20.0,
                  refetch_failed: bool = False) -> pl.DataFrame:
    """yfinance .info for each ticker (cached in registry/yf_info.parquet). ~1 ticker/s with pauses (research doc).
    refetch_failed=True also retries cached rows with ok=False (e.g. throttled by Yahoo)."""
    s = settings or get_settings()
    cache = _cache_read("yf_info", s)
    if cache is None:
        cache = pl.DataFrame(schema=_YF_SCHEMA)
    want = list(dict.fromkeys(t for t in tickers if t))
    fresh = cache.filter(pl.col("fetched_at") >= datetime.now(UTC) - timedelta(days=max_age_days))
    if refetch_failed:
        fresh = fresh.filter(pl.col("ok").fill_null(False))
    have = set(fresh.get_column("yf_ticker").to_list())
    todo = [t for t in want if t not in have]
    if budget is not None:
        todo = todo[:budget]
    if todo:
        try:
            import yfinance as yf
        except ImportError:  # pragma: no cover
            log.warning("yfinance not installed")
            return cache.filter(pl.col("yf_ticker").is_in(want))
        lim = get_limiter("yfinance_fundamentals")
        rows = []
        fails = 0
        for i, t in enumerate(todo):
            lim.acquire()
            now = datetime.now(UTC)
            try:
                info = yf.Ticker(t).info or {}
                rec: dict[str, Any] = {"yf_ticker": t, "ok": bool(info.get("quoteType")), "fetched_at": now}
                for k in _YF_INFO_KEYS:
                    v = info.get(k)
                    if k == "longBusinessSummary" and v:
                        v = str(v)[:600]
                    if k == "isin" and v in ("-", ""):
                        v = None
                    rec[k] = v
                rows.append(rec)
                fails = 0
            except Exception as e:  # noqa: BLE001
                fails += 1
                rows.append({"yf_ticker": t, "ok": False, "fetched_at": now})
                log.debug("yf info %s: %s", t, e)
                if "Too Many Requests" in str(e) or "Rate" in type(e).__name__:
                    log.warning("yfinance rate-limited; sleeping 120s")
                    time.sleep(120)
                if fails >= 25:
                    log.warning("yfinance: 25 consecutive failures, stopping info fetch")
                    break
            if i and i % pause_every == 0:
                log.info("yf info: %d/%d", i, len(todo))
                _cache_write(_merge_yf(cache, rows), "yf_info", s)
                time.sleep(pause_s)
        cache = _merge_yf(cache, rows)
        _cache_write(cache, "yf_info", s)
    return cache.filter(pl.col("yf_ticker").is_in(want))


def _merge_yf(cache: pl.DataFrame, rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return cache
    clean = []
    for r in rows:
        x = {k: r.get(k) for k in _YF_SCHEMA}
        for k in ("marketCap", "regularMarketPrice", "previousClose", "sharesOutstanding"):
            try:
                x[k] = float(x[k]) if x[k] is not None else None
            except (TypeError, ValueError):
                x[k] = None
        try:
            x["fullTimeEmployees"] = int(x["fullTimeEmployees"]) if x["fullTimeEmployees"] is not None else None
        except (TypeError, ValueError):
            x["fullTimeEmployees"] = None
        for k, t in _YF_SCHEMA.items():
            if t == pl.Utf8 and x[k] is not None:
                x[k] = str(x[k])
        clean.append(x)
    add = pl.DataFrame(clean, schema=_YF_SCHEMA)
    return pl.concat([cache, add]).unique(subset=["yf_ticker"], keep="last", maintain_order=True)


@_safe
def fetch_fx_spot(currencies: Iterable[str], settings: Settings | None = None) -> pl.DataFrame:
    """USD per unit for each currency: fx_rates table if populated (latest), else yfinance 'XXXUSD=X' last close."""
    ccys = sorted({c.upper() for c in currencies if c and c.upper() not in ("USD",)})
    out = {"USD": 1.0}
    try:
        from app.core.storage import read_table

        fx = read_table("fx_rates", filters=pl.col("date") >= _today() - timedelta(days=15))
        if fx.height:
            last = fx.sort("date").group_by("currency").agg(pl.col("usd_per_unit").last())
            out.update(dict(zip(last.get_column("currency"), last.get_column("usd_per_unit"), strict=True)))
    except Exception:  # noqa: BLE001
        pass
    missing = [c for c in ccys if c not in out]
    if missing:
        import yfinance as yf

        syms = [f"{c}USD=X" for c in missing]
        get_limiter("yfinance").acquire()
        data = yf.download(syms, period="7d", progress=False, auto_adjust=False, threads=False)
        closes = data["Close"] if "Close" in data else data
        for c, sym in zip(missing, syms, strict=True):
            try:
                ser = closes[sym].dropna() if sym in closes else closes.dropna()
                if len(ser):
                    out[c] = float(ser.iloc[-1])
            except Exception:  # noqa: BLE001
                continue
    gbx = out.get("GBP")
    if gbx:
        out["GBP"] = gbx
    return pl.DataFrame({"currency": list(out), "usd_per_unit": list(out.values())})


# ------------------------------------------------------------------------------------------------ Japan


@_safe
def fetch_jpx_list(settings: Settings | None = None) -> pl.DataFrame:
    """JPX listed-company list: jp_sec_code, name_ja, market, tse33_code, tse33_name, tse17_code, size_code."""
    import pandas as pd

    h = _client("jpx", settings, headers={"User-Agent": _BROWSER_UA}, rate_limit=RateLimit(1.0, 1))
    page = h.get("https://www.jpx.co.jp/markets/statistics-equities/misc/01.html", cache_ttl=7 * 86400).text
    m = re.search(r'href="([^"]+data_j\.xlsx)"', page)
    url = ("https://www.jpx.co.jp" + m.group(1)) if m and m.group(1).startswith("/") else (
        m.group(1) if m else "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx")
    r = h.get(url, cache_ttl=7 * 86400)
    land_raw("jpx", "listed_companies", "data_j.xlsx", r.content, settings, compress=False)
    pdf = pd.read_excel(io.BytesIO(r.content), dtype=str)
    cols = list(pdf.columns)
    ren = {cols[1]: "jp_sec_code", cols[2]: "name_ja", cols[3]: "market", cols[4]: "tse33_code", cols[5]: "tse33_name",
           cols[6]: "tse17_code", cols[8]: "size_code"}
    pdf = pdf.rename(columns=ren)[list(ren.values())]
    return pl.from_pandas(pdf).with_columns(
        pl.col("jp_sec_code").str.strip_chars(),
        pl.when(pl.col("tse33_code").str.contains(r"^\d+$")).then(pl.col("tse33_code").str.zfill(4))
        .otherwise(None).alias("tse33_code"),
    )


@_safe
def fetch_edinet_codelist(settings: Settings | None = None) -> pl.DataFrame:
    """EDINET code list: edinet_code, jp_sec_code (4-char), name_ja, name_en, industry_ja, fye, listed, consolidated."""
    h = _client("edinet", settings, rate_limit=RateLimit(1.0, 1))
    r = h.get("https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip", cache_ttl=7 * 86400)
    land_raw("edinet", "codelist", "Edinetcode.zip", r.content, settings, compress=False)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    text = z.read(name).decode("cp932", errors="replace")
    lines = text.splitlines()[1:]  # first line is metadata
    rdr = csv.reader(lines)
    header = next(rdr)
    rows = [dict(zip(header, x, strict=False)) for x in rdr]
    keys = list(header)
    def col(i: int) -> list[str | None]:
        return [_str(r.get(keys[i])) for r in rows]
    df = pl.DataFrame({
        "edinet_code": col(0), "filer_type": col(1), "listed": col(2), "consolidated": col(3), "fye": col(5),
        "name_ja": col(6), "name_en": col(7), "industry_ja": col(10), "sec_code5": col(11), "corp_number": col(12),
    })
    return df.with_columns(
        pl.when(pl.col("sec_code5").str.len_chars() == 5).then(pl.col("sec_code5").str.slice(0, 4))
        .otherwise(pl.col("sec_code5")).alias("jp_sec_code")
    )


# ------------------------------------------------------------------------------------------------ Korea


@_safe
def fetch_dart_corpcodes(settings: Settings | None = None) -> pl.DataFrame:
    """OpenDART corpCode.xml: dart_corp_code, corp_name, kr_stock_code, modify_date (listed only)."""
    from lxml import etree

    s = settings or get_settings()
    key = s.secret("opendart_apikey")
    if not key:
        log.info("OPENDART key missing: skip corpCode")
        return pl.DataFrame()
    h = _client("opendart", s)
    r = h.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, cache_ttl=7 * 86400)
    if not r.content.startswith(b"PK"):
        log.warning("corpCode.xml: unexpected payload (%s)", r.content[:120])
        return pl.DataFrame()
    land_raw("opendart", "corpcode", "corpCode.zip", r.content, s, compress=False)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = etree.fromstring(z.read(z.namelist()[0]))
    rows = []
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if not sc:
            continue
        rows.append({"dart_corp_code": (el.findtext("corp_code") or "").strip(), "corp_name": el.findtext("corp_name"),
                     "corp_name_eng": el.findtext("corp_eng_name"), "kr_stock_code": sc,
                     "modify_date": el.findtext("modify_date")})
    return pl.DataFrame(rows)


def fetch_dart_company(corp_codes: Iterable[str], settings: Settings | None = None, max_age_days: int = 60) -> pl.DataFrame:
    """OpenDART company.json per corp_code (cached): induty_code (KSIC), acc_mt, corp_name_eng, jurir_no, hm_url, adres."""
    s = settings or get_settings()
    schema = {"dart_corp_code": pl.Utf8, "induty_code": pl.Utf8, "acc_mt": pl.Utf8, "corp_name_eng": pl.Utf8,
              "corp_name": pl.Utf8, "jurir_no": pl.Utf8, "hm_url": pl.Utf8, "corp_cls": pl.Utf8,
              "fetched_at": pl.Datetime("us", "UTC")}
    cache = _cache_read("dart_company", s)
    if cache is None:
        cache = pl.DataFrame(schema=schema)
    key = s.secret("opendart_apikey")
    want = [c for c in dict.fromkeys(corp_codes) if c]
    fresh = cache.filter(pl.col("fetched_at") >= datetime.now(UTC) - timedelta(days=max_age_days))
    todo = [c for c in want if c not in set(fresh.get_column("dart_corp_code").to_list())]
    if todo and key:
        h = _client("opendart", s)
        rows = []
        for c in todo:
            try:
                j = h.get("https://opendart.fss.or.kr/api/company.json", params={"crtfc_key": key, "corp_code": c},
                          cache_ttl=max_age_days * 86400).json()
                if j.get("status") != "000":
                    continue
                rows.append({k: _str(j.get(k)) for k in schema if k not in ("dart_corp_code", "fetched_at")}
                            | {"dart_corp_code": c, "fetched_at": datetime.now(UTC)})
            except Exception as e:  # noqa: BLE001
                log.warning("dart company %s: %s", c, e)
        if rows:
            cache = pl.concat([cache, pl.DataFrame(rows, schema=schema)]).unique(subset=["dart_corp_code"], keep="last")
            _cache_write(cache, "dart_company", s)
    return cache.filter(pl.col("dart_corp_code").is_in(want))


@_safe
def fetch_kind_list(settings: Settings | None = None) -> pl.DataFrame:
    """KRX KIND listed companies: kr_stock_code, name_ko, market, industry_ko, fiscal_month."""
    import pandas as pd

    h = _client("krx", settings, headers={"User-Agent": _BROWSER_UA}, rate_limit=RateLimit(1.0, 1))
    r = h.get("https://kind.krx.co.kr/corpgeneral/corpList.do", params={"method": "download", "searchType": "13"},
              cache_ttl=7 * 86400)
    land_raw("krx", "kind_corplist", "corpList.html", r.content, settings)
    pdf = pd.read_html(io.StringIO(r.content.decode("euc-kr", errors="replace")))[0].astype(str)
    c = list(pdf.columns)
    out = pl.DataFrame({"name_ko": pdf[c[0]].tolist(), "market": pdf[c[1]].tolist(),
                        "kr_stock_code": [x.zfill(6) for x in pdf[c[2]].tolist()],
                        "industry_ko": pdf[c[3]].tolist(), "fiscal_month": pdf[c[6]].tolist()})
    return out


# ------------------------------------------------------------------------------------------------ China / HK


@_safe
def fetch_sse_list(settings: Settings | None = None) -> pl.DataFrame:
    """SSE A-share list: cn_code, name_cn, name_en, csrc_code."""
    h = _client("sse", settings, headers={"Referer": "https://www.sse.com.cn/", "User-Agent": _BROWSER_UA},
                rate_limit=RateLimit(1.0, 1))
    rows: list[dict[str, Any]] = []
    for page in range(1, 20):
        r = h.get("https://query.sse.com.cn/sseQuery/commonQuery.do", params={
            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L", "STOCK_TYPE": "1", "COMPANY_STATUS": "2,4,5,7,8",
            "type": "inParams", "isPagination": "true", "pageHelp.pageSize": "500", "pageHelp.pageNo": str(page),
            "pageHelp.beginPage": str(page), "pageHelp.endPage": str(page)}, cache_ttl=7 * 86400)
        txt = r.text.strip()
        if txt.startswith("jsonp") or not txt.startswith("{"):
            txt = txt[txt.find("{"): txt.rfind("}") + 1]
        res = json.loads(txt).get("result") or []
        if not res:
            break
        rows.extend(res)
        if len(res) < 500:
            break
    land_raw("sse", "stock_list", "sse_list.json", json.dumps(rows, ensure_ascii=False).encode("utf-8"), settings)
    return pl.DataFrame({
        "cn_code": ["SSE:" + str(x.get("A_STOCK_CODE") or x.get("COMPANY_CODE")) for x in rows],
        "name_cn": [x.get("SEC_NAME_CN") or x.get("COMPANY_ABBR") for x in rows],
        "name_en": [x.get("FULL_NAME_IN_ENGLISH") or x.get("ENGLISH_ABBR") for x in rows],
        "csrc_code": [x.get("CSRC_CODE") for x in rows],
    }).unique(subset=["cn_code"], keep="first", maintain_order=True)


@_safe
def fetch_szse_list(settings: Settings | None = None) -> pl.DataFrame:
    """SZSE A-share list: cn_code, name_cn, name_en, csrc_industry (text 'C 制造业' or code)."""
    import pandas as pd

    h = _client("szse", settings, headers={"User-Agent": _BROWSER_UA}, rate_limit=RateLimit(1.0, 1))
    r = h.get("https://www.szse.cn/api/report/ShowReport",
              params={"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1"}, cache_ttl=7 * 86400)
    land_raw("szse", "stock_list", "szse_list.xlsx", r.content, settings, compress=False)
    pdf = pd.read_excel(io.BytesIO(r.content), dtype=str)
    code_col = next(c for c in pdf.columns if "A股代码" in str(c))
    name_col = next((c for c in pdf.columns if "A股简称" in str(c)), code_col)
    en_col = next((c for c in pdf.columns if "英文" in str(c)), None)
    ind_col = next((c for c in pdf.columns if "所属行业" in str(c)), None)
    return pl.DataFrame({
        "cn_code": ["SZSE:" + str(x).zfill(6) for x in pdf[code_col].tolist()],
        "name_cn": pdf[name_col].astype(str).tolist(),
        "name_en": pdf[en_col].astype(str).tolist() if en_col else [None] * len(pdf),
        "csrc_code": pdf[ind_col].astype(str).tolist() if ind_col else [None] * len(pdf),
    })


@_safe
def fetch_hkex_list(settings: Settings | None = None) -> pl.DataFrame:
    """HKEX ListOfSecurities.xlsx (equities): hk_code (5-digit), name, sub_category, board_lot, isin."""
    import pandas as pd

    h = _client("hkex", settings, headers={"User-Agent": _BROWSER_UA}, rate_limit=RateLimit(1.0, 1))
    r = h.get("https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx",
              cache_ttl=7 * 86400)
    land_raw("hkex", "list_of_securities", "ListOfSecurities.xlsx", r.content, settings, compress=False)
    pdf = pd.read_excel(io.BytesIO(r.content), header=2, dtype=str)
    pdf = pdf[pdf["Category"] == "Equity"]
    lot_col = next(c for c in pdf.columns if "Board Lot" in str(c))
    return pl.DataFrame({
        "hk_code": [str(x).zfill(5) for x in pdf["Stock Code"].tolist()],
        "name": pdf["Name of Securities"].tolist(),
        "sub_category": pdf["Sub-Category"].tolist() if "Sub-Category" in pdf else [None] * len(pdf),
        "board_lot": [int(str(x).replace(",", "")) if str(x).replace(",", "").isdigit() else None for x in pdf[lot_col]],
        "isin": pdf["ISIN"].tolist() if "ISIN" in pdf else [None] * len(pdf),
    })


# ------------------------------------------------------------------------------------------------ Brazil


@_safe
def fetch_cvm_cadastro(settings: Settings | None = None) -> pl.DataFrame:
    """CVM cad_cia_aberta.csv: cvm_code, cnpj, name, situation, sector."""
    h = _client("cvm", settings)
    r = h.get("https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv", cache_ttl=7 * 86400)
    land_raw("cvm", "cadastro", "cad_cia_aberta.csv", r.content, settings)
    rows = list(csv.DictReader(io.StringIO(r.content.decode("latin-1")), delimiter=";"))
    return pl.DataFrame({
        "cvm_code": [str(int(x["CD_CVM"])) if (x.get("CD_CVM") or "").strip().isdigit() else None for x in rows],
        "cnpj": [re.sub(r"\D", "", x.get("CNPJ_CIA") or "").zfill(14) for x in rows],
        "name": [x.get("DENOM_SOCIAL") for x in rows],
        "name_trade": [x.get("DENOM_COMERC") for x in rows],
        "situation": [x.get("SIT") for x in rows],
        "sector": [x.get("SETOR_ATIV") for x in rows],
        "registered": [x.get("DT_REG") for x in rows],
    })


@_safe
def fetch_cvm_tickers(settings: Settings | None = None, year: int | None = None) -> pl.DataFrame:
    """CVM FCA valor_mobiliario: cnpj, ticker (Codigo_Negociacao), security_type, segment, market."""
    h = _client("cvm", settings)
    y = year or _today().year
    frames: list[pl.DataFrame] = []
    for yy in (y, y - 1, y - 2):
        try:
            r = h.get(f"https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{yy}.zip",
                      cache_ttl=7 * 86400)
        except HttpError:
            continue
        land_raw("cvm", "fca", f"fca_cia_aberta_{yy}.zip", r.content, settings, compress=False)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        name = next((n for n in z.namelist() if "valor_mobiliario" in n), None)
        if not name:
            continue
        rows = list(csv.DictReader(io.StringIO(z.read(name).decode("latin-1")), delimiter=";"))
        frames.append(pl.DataFrame({
            "cnpj": [re.sub(r"\D", "", x.get("CNPJ_Companhia") or "").zfill(14) for x in rows],
            "ticker": [_str(x.get("Codigo_Negociacao")) for x in rows],
            "security_type": [x.get("Valor_Mobiliario") for x in rows],
            "segment": [x.get("Segmento") for x in rows],
            "market": [x.get("Mercado") for x in rows],
            "end_date": [_str(x.get("Data_Fim_Negociacao")) for x in rows],
            "fca_year": [yy] * len(rows),
        }).filter(pl.col("ticker").is_not_null()))
    if not frames:
        return pl.DataFrame()
    # newest FCA year wins per ticker
    return pl.concat(frames).sort("fca_year", descending=True).unique(subset=["ticker"], keep="first")


@_safe
def fetch_b3_isins(settings: Settings | None = None, day: date | None = None) -> pl.DataFrame:
    """B3 COTAHIST daily file: ticker <-> ISIN (CODISI) for every B3 instrument traded that day."""
    h = _client("b3", settings)
    d = day or (_today() - timedelta(days=1))
    for _ in range(7):
        if d.weekday() < 5:
            url = f"https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_D{d.strftime('%d%m%Y')}.ZIP"
            try:
                r = h.get(url, cache_ttl=-1)
                if r.content[:2] == b"PK":
                    land_raw("b3", "cotahist_daily", f"COTAHIST_D{d.strftime('%d%m%Y')}.ZIP", r.content, settings,
                             compress=False)
                    z = zipfile.ZipFile(io.BytesIO(r.content))
                    txt = z.read(z.namelist()[0]).decode("latin-1")
                    rows = [(ln[12:24].strip(), ln[230:242].strip(), ln[10:12]) for ln in txt.splitlines()
                            if ln.startswith("01")]
                    return pl.DataFrame({"ticker": [a for a, _, _ in rows], "isin": [b for _, b, _ in rows],
                                         "bdi": [c for _, _, c in rows]}).unique(subset=["ticker"])
            except HttpError:
                pass
        d -= timedelta(days=1)
    return pl.DataFrame()


# ------------------------------------------------------------------------------------------------ Europe


@_safe
def fetch_firds_equities(settings: Settings | None = None, max_age_days: int = 7) -> pl.DataFrame:
    """ESMA FIRDS FULINS_E (weekly full file, all parts): isin, full_name, short_name, cfi, currency, issuer_lei,
    venue_mic (most relevant venue), first_trade. Shares (CFI 'ES*') and DRs ('ED*') only. Cached 7 days."""
    s = settings or get_settings()
    cache_p = registry_dir(s) / "firds_equities.parquet"
    if cache_p.exists() and datetime.fromtimestamp(cache_p.stat().st_mtime, UTC) > datetime.now(UTC) - timedelta(days=max_age_days):
        return pl.read_parquet(cache_p)
    from lxml import etree

    h = _client("esma_firds", s, rate_limit=RateLimit(1.0, 1))
    end = _today()
    start = end - timedelta(days=13)
    q = (f"https://registers.esma.europa.eu/solr/esma_registers_firds_files/select?q=*&fq=publication_date:"
         f"[{start.isoformat()}T00:00:00Z TO {end.isoformat()}T23:59:59Z]&wt=json&rows=500")
    docs = h.get(q, cache_ttl=6 * 3600).json()["response"]["docs"]
    full = sorted([d for d in docs if str(d.get("file_name", "")).startswith("FULINS_E_")],
                  key=lambda d: d["file_name"], reverse=True)
    if not full:
        log.warning("FIRDS: no FULINS_E file in the last 2 weeks")
        return pl.DataFrame()
    latest_date = full[0]["file_name"].split("_")[2]
    parts = sorted([d for d in full if d["file_name"].split("_")[2] == latest_date], key=lambda d: d["file_name"])
    recs: dict[str, dict[str, Any]] = {}
    for d in parts:
        dest = s.raw_dir / "esma_firds" / "ref_fulins_e" / f"dt={_today().isoformat()}" / d["file_name"]
        if not dest.exists():
            h.download(d["download_link"], dest)
        with zipfile.ZipFile(dest) as z:
            xml_name = z.namelist()[0]
            with z.open(xml_name) as fh:
                for _, el in etree.iterparse(fh, events=("end",), tag="{*}RefData"):
                    g = el.find("{*}FinInstrmGnlAttrbts")
                    isin = g.findtext("{*}Id") if g is not None else None
                    cfi = g.findtext("{*}ClssfctnTp") if g is not None else None
                    if isin and cfi and cfi[:2] in ("ES", "ED"):
                        tech = el.find("{*}TechAttrbts")
                        venue = el.find("{*}TradgVnRltdAttrbts")
                        rel = tech.findtext("{*}RlvntTradgVn") if tech is not None else None
                        r = recs.get(isin)
                        if r is None:
                            recs[isin] = {
                                "isin": isin, "full_name": g.findtext("{*}FullNm"), "short_name": g.findtext("{*}ShrtNm"),
                                "cfi": cfi, "currency": g.findtext("{*}NtnlCcy"), "issuer_lei": el.findtext("{*}Issr"),
                                "venue_mic": rel, "first_trade": (venue.findtext("{*}FrstTradDt") if venue is not None else None),
                            }
                    el.clear()
                    while el.getprevious() is not None:
                        del el.getparent()[0]
    df = pl.DataFrame(list(recs.values())) if recs else pl.DataFrame()
    if df.height:
        df.write_parquet(cache_p)
    log.info("FIRDS equities: %d ISINs", df.height)
    return df


def fetch_gleif_records(leis: Iterable[str], settings: Settings | None = None, max_age_days: int = 30) -> pl.DataFrame:
    """GLEIF lei-records (batched filter[lei], 200 per call, cached): lei, legal_name, country, jurisdiction,
    registered_as, status, legal_form."""
    s = settings or get_settings()
    schema = {"lei": pl.Utf8, "legal_name": pl.Utf8, "country": pl.Utf8, "jurisdiction": pl.Utf8,
              "registered_as": pl.Utf8, "status": pl.Utf8, "other_names": pl.List(pl.Utf8),
              "fetched_at": pl.Datetime("us", "UTC")}
    cache = _cache_read("gleif_records", s)
    if cache is None:
        cache = pl.DataFrame(schema=schema)
    want = sorted({x.strip().upper() for x in leis if x and len(x.strip()) == 20})
    fresh = cache.filter(pl.col("fetched_at") >= datetime.now(UTC) - timedelta(days=max_age_days))
    todo = [x for x in want if x not in set(fresh.get_column("lei").to_list())]
    if todo:
        h = _client("gleif", s)
        rows = []
        for i in range(0, len(todo), 100):
            chunk = todo[i:i + 100]
            try:
                j = h.get("https://api.gleif.org/api/v1/lei-records",
                          params={"filter[lei]": ",".join(chunk), "page[size]": 200}, cache_ttl=max_age_days * 86400).json()
            except HttpError as e:
                log.warning("gleif batch failed: HTTP %s", e.status)
                continue
            now = datetime.now(UTC)
            for rec in j.get("data") or []:
                a = rec.get("attributes") or {}
                ent = a.get("entity") or {}
                rows.append({
                    "lei": a.get("lei"), "legal_name": (ent.get("legalName") or {}).get("name"),
                    "country": (ent.get("legalAddress") or {}).get("country"), "jurisdiction": ent.get("jurisdiction"),
                    "registered_as": ent.get("registeredAs"), "status": ent.get("status"),
                    "other_names": [n.get("name") for n in (ent.get("otherNames") or []) if n.get("name")],
                    "fetched_at": now,
                })
        if rows:
            cache = pl.concat([cache, pl.DataFrame(rows, schema=schema)]).unique(subset=["lei"], keep="last")
            _cache_write(cache, "gleif_records", s)
    return cache.filter(pl.col("lei").is_in(want))


def gleif_lei_by_isin(isins: Iterable[str], settings: Settings | None = None, max_age_days: int = 90,
                      budget: int = 400) -> dict[str, str]:
    """ISIN -> issuer LEI via GLEIF `lei-records?filter[isin]=` (exact; one call per ISIN, shared 'gleif' limiter,
    cached in registry/gleif_isin.parquet incl. negative results)."""
    s = settings or get_settings()
    schema = {"isin": pl.Utf8, "lei": pl.Utf8, "fetched_at": pl.Datetime("us", "UTC")}
    cache = _cache_read("gleif_isin", s)
    if cache is None:
        cache = pl.DataFrame(schema=schema)
    want = sorted({i.strip().upper() for i in isins if i and is_valid_isin(i.strip().upper())})
    fresh = cache.filter(pl.col("fetched_at") >= datetime.now(UTC) - timedelta(days=max_age_days))
    known = set(fresh.get_column("isin").to_list())
    todo = [i for i in want if i not in known][:budget]
    if todo:
        h = _client("gleif", s, rate_limit=RateLimit.per_minute(50, 1))
        rows = []
        for isin in todo:
            try:
                j = h.get("https://api.gleif.org/api/v1/lei-records", params={"filter[isin]": isin, "page[size]": 5},
                          cache_ttl=max_age_days * 86400).json()
                data = j.get("data") or []
                lei = (data[0].get("attributes") or {}).get("lei") if len(data) == 1 else None
            except HttpError as e:
                log.warning("gleif isin %s: HTTP %s", isin, e.status)
                continue
            rows.append({"isin": isin, "lei": lei, "fetched_at": datetime.now(UTC)})
        if rows:
            cache = pl.concat([cache, pl.DataFrame(rows, schema=schema)]).unique(subset=["isin"], keep="last")
            _cache_write(cache, "gleif_isin", s)
    hit = cache.filter(pl.col("isin").is_in(want) & pl.col("lei").is_not_null())
    return dict(zip(hit.get_column("isin").to_list(), hit.get_column("lei").to_list(), strict=True))


_CJK_SUFFIX = re.compile(r"(株式会社|（株）|\(株\)|주식회사|\(주\)|㈜|股份有限公司|集团股份有限公司|有限公司|股份公司|集團|集团)")


def _name_score(a: str, b: str) -> float:
    """Name similarity working for Latin (normalize_name + token_sort_ratio) and CJK (compact string, suffixes removed)."""
    from rapidfuzz import fuzz

    from app.normalize.identity import normalize_name

    na, nb = normalize_name(a), normalize_name(b)
    best = fuzz.token_sort_ratio(na, nb) if na and nb else 0.0
    ca = re.sub(r"[\s　・.,()（）]", "", _CJK_SUFFIX.sub("", a or ""))
    cb = re.sub(r"[\s　・.,()（）]", "", _CJK_SUFFIX.sub("", b or ""))
    if ca and cb and (not na or not nb):
        best = max(best, fuzz.ratio(ca, cb))
    return float(best)


def gleif_lei_by_name(queries: list[tuple[str, str, list[str]]], settings: Settings | None = None,
                      max_age_days: int = 90, min_score: float = 95.0) -> dict[str, str]:
    """Strict name fallback: queries = [(key, country ISO2, [names...])]. GLEIF fuzzycompletions per name, candidate
    records verified: ACTIVE entity, legal-address or jurisdiction country == country, and rapidfuzz token_sort_ratio
    of normalized names >= min_score. Returns {key: lei} for unambiguous hits (cached in registry/gleif_name.parquet)."""
    from rapidfuzz import fuzz

    from app.normalize.identity import normalize_name

    s = settings or get_settings()
    schema = {"q": pl.Utf8, "lei": pl.Utf8, "fetched_at": pl.Datetime("us", "UTC")}
    cache = _cache_read("gleif_name", s)
    if cache is None:
        cache = pl.DataFrame(schema=schema)
    fresh = cache.filter(pl.col("fetched_at") >= datetime.now(UTC) - timedelta(days=max_age_days))
    memo = dict(zip(fresh.get_column("q").to_list(), fresh.get_column("lei").to_list(), strict=True))
    h = _client("gleif", s, rate_limit=RateLimit.per_minute(50, 1))
    out: dict[str, str] = {}
    new_rows = []
    for key, cc, names in queries:
        qk = f"{cc}|{key}"
        if qk in memo:
            if memo[qk]:
                out[key] = memo[qk]
            continue
        cands: set[str] = set()
        for n in [x for x in names if x][:3]:
            try:
                j = h.get("https://api.gleif.org/api/v1/fuzzycompletions", params={"field": "entity.legalName", "q": n},
                          cache_ttl=max_age_days * 86400).json()
            except HttpError:
                continue
            for d in j.get("data") or []:
                lei = ((d.get("relationships") or {}).get("lei-records") or {}).get("data", {}).get("id")
                if lei:
                    cands.add(lei)
        hit = None
        if cands:
            recs = fetch_gleif_records(sorted(cands), settings=s)
            good = []
            for r in recs.iter_rows(named=True):
                ctry = {(r.get("country") or "").upper(), (r.get("jurisdiction") or "")[:2].upper()}
                if (r.get("status") or "").upper() != "ACTIVE" or cc.upper() not in ctry:
                    continue
                gnames = [r.get("legal_name")] + list(r.get("other_names") or [])
                sc = max(_name_score(g, n) for g in gnames if g for n in names if n)
                if sc >= min_score:
                    good.append((sc, r["lei"]))
            good.sort(reverse=True)
            if good and (len(good) == 1 or good[0][0] > good[1][0]):
                hit = good[0][1]
        new_rows.append({"q": qk, "lei": hit, "fetched_at": datetime.now(UTC)})
        if hit:
            out[key] = hit
    if new_rows:
        cache = pl.concat([cache, pl.DataFrame(new_rows, schema=schema)]).unique(subset=["q"], keep="last")
        _cache_write(cache, "gleif_name", s)
    return out


def gleif_search_name(name: str, country: str | None = None, settings: Settings | None = None) -> list[dict[str, Any]]:
    """GLEIF fuzzy completions on legal name -> [{lei, name}] (used as LEI fallback for seeds without FIRDS match)."""
    h = _client("gleif", settings)
    try:
        j = h.get("https://api.gleif.org/api/v1/fuzzycompletions",
                  params={"field": "entity.legalName", "q": name}, cache_ttl=30 * 86400).json()
    except HttpError:
        return []
    out = []
    for d in j.get("data") or []:
        lei = ((d.get("relationships") or {}).get("lei-records") or {}).get("data", {}).get("id")
        out.append({"lei": lei, "name": (d.get("attributes") or {}).get("value")})
    return [x for x in out if x["lei"]]


# ------------------------------------------------------------------------------------------------ KR ISIN

def isin_check_digit(body11: str) -> str:
    """ISO 6166 check digit for the first 11 chars."""
    digits = "".join(str(int(c, 36)) for c in body11.upper())
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            n = n - 9 if n > 9 else n
        total += n
    return str((10 - total % 10) % 10)


def is_valid_isin(isin: str | None) -> bool:
    return bool(isin and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin) and isin_check_digit(isin[:11]) == isin[11])


def kr_common_isin(stock_code: str) -> str | None:
    """KRX common-share ISIN: 'KR7' + 6-digit code + '00' + check digit (005930 -> KR7005930003).
    Only valid for ordinary shares whose code ends in 0."""
    if not re.fullmatch(r"\d{6}", stock_code or "") or not stock_code.endswith("0"):
        return None
    body = f"KR7{stock_code}00"
    return body + isin_check_digit(body)


# ------------------------------------------------------------------------------------------------ jobs


def run(ctx: Any) -> Any:
    """ref_universe (nightly) / universe_rebalance (monthly, params.mode='rebalance').

    params: mode ('nightly'|'rebalance'|'full'), refresh_yf (bool), max_submissions (int), us_size (int),
    keep_universes ('mvp_us,mvp_global': carry their current membership over unchanged on a rebalance)."""
    from app.normalize import identity
    from app.scheduler.base import JobResult

    params = dict(ctx.params or {})
    mode = params.get("mode", "nightly")
    res = identity.build_reference(
        as_of=ctx.as_of, run_id=ctx.run_id, settings=ctx.settings,
        write_universe=(mode in ("rebalance", "full")) or params.get("write_universe", None),
        us_size=int(params.get("us_size", 500)), max_submissions=params.get("max_submissions"),
        fetch_yf=bool(params.get("refresh_yf", True)), cancel=ctx.check_cancelled,
        keep_universes=tuple(x.strip() for x in str(params.get("keep_universes") or "").split(",") if x.strip()),
    )
    return JobResult(status="success" if not res.get("warnings") else "partial", rows_written=res["rows"],
                     entities_ok=res.get("issuers", 0), message="; ".join(res.get("warnings", []))[:2000] or None)
