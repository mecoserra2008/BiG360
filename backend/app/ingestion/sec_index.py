"""SEC EDGAR filing index (job `sec_filings_index`) + shared SEC helpers used by the other sec_* adapters.

Source: EDGAR daily index `daily-index/YYYY/QTRn/master.YYYYMMDD.idx` (pipe-delimited, published ~22:00 ET) for
incremental runs, quarterly `full-index/YYYY/QTRn/master.idx` for backfills. Rows are enriched for universe CIKs with
the submissions API (`acceptanceDateTime` -> knowledge_ts, reportDate -> period_end, primaryDocument -> url, 8-K items,
isXBRL).

Writes: filings_index (source_id = sec_edgar, filing_id = "sec_edgar:<accession>", knowledge_date = Date Filed).
Watermark: last index date processed (ISO date), persisted in data/state/sec_index.json and returned to job_runs.

All SEC adapters share ONE rate limiter ("sec_edgar", 8 req/s) and the SEC User-Agent from .env, so the parallel
nightly jobs (sec_xbrl_facts, sec_form4, sec_text) together stay under the 10 req/s fair-access cap.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient, HttpError
from app.core.logsetup import get_logger
from app.core.ratelimit import RateLimit
from app.core.storage import read_table, scan_table
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

log = get_logger("app.ingestion.sec")

SEC_SOURCE = "sec_edgar"
SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
ARCHIVES = f"{SEC_WWW}/Archives/edgar"
ET = ZoneInfo("America/New_York")
SEC_RATE = RateLimit(8.0, 8)

# Forms kept in filings_index (base form, amendments included). Everything else (424B2 notes, D, 497, 144, ...)
# is skipped unless params.all_forms is true.
INDEX_FORMS = {
    "10-K", "10-K405", "10-KT", "10-Q", "10-QT", "20-F", "40-F", "6-K", "8-K", "3", "4", "5",
    "13F-HR", "13F-NT", "DEF 14A", "DEFA14A", "SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G",
    "S-1", "F-1", "S-4", "F-4", "10-12B", "20-F/A", "NT 10-K", "NT 10-Q", "NT 20-F", "11-K", "ARS",
}
ANNUAL = {"10-K", "10-K405", "10-KT", "20-F", "40-F"}
QUARTERLY = {"10-Q", "10-QT"}
XBRL_FORMS = ANNUAL | QUARTERLY

# Fallback MVP universe extras (ADR / foreign filers named in the build brief)
ADR_EXTRAS = ("PBR", "NU", "TSM", "SAP", "TM")

COUNTRY_REGION = {
    "Brazil": "LATAM", "Mexico": "LATAM", "Argentina": "LATAM", "Chile": "LATAM", "Colombia": "LATAM",
    "Peru": "LATAM", "Uruguay": "LATAM", "Panama": "LATAM", "Canada": "CA", "United Kingdom": "UK",
    "Jersey": "UK", "Guernsey": "UK", "Isle of Man": "UK", "Japan": "JP", "Korea, Republic of": "KR",
    "South Korea": "KR", "China": "CN", "Hong Kong": "HK", "Taiwan": "APAC", "Taiwan, Province of China": "APAC",
    "India": "APAC", "Singapore": "APAC", "Australia": "APAC", "Indonesia": "APAC", "Philippines": "APAC",
    "Thailand": "APAC", "Malaysia": "APAC", "New Zealand": "APAC", "Germany": "EU", "France": "EU",
    "Netherlands": "EU", "Switzerland": "EU", "Ireland": "EU", "Spain": "EU", "Italy": "EU", "Belgium": "EU",
    "Luxembourg": "EU", "Denmark": "EU", "Sweden": "EU", "Norway": "EU", "Finland": "EU", "Austria": "EU",
    "Portugal": "EU", "Greece": "EU", "Poland": "EU", "Israel": "OTHER", "South Africa": "OTHER",
}

DISSEMINATION_LAG_DAYS = 5

EXCHANGE_MIC = {"Nasdaq": "XNAS", "NYSE": "XNYS", "CBOE": "BATS", "OTC": "OTCM", "TXSE": "TXSE"}


# ----------------------------------------------------------------------------------------------- small helpers


def cik10(x: Any) -> str:
    return str(int(str(x).strip())).zfill(10)


def acc_nodash(acc: str) -> str:
    return acc.replace("-", "")


def make_filing_id(acc: str) -> str:
    return f"{SEC_SOURCE}:{acc}"


def base_form(form: str) -> str:
    f = (form or "").strip().upper()
    return f[:-2] if f.endswith("/A") else f


def is_amendment(form: str) -> bool:
    return (form or "").strip().upper().endswith("/A")


def form_class(form: str) -> str:
    b = base_form(form)
    if b in ANNUAL:
        return "annual"
    if b in QUARTERLY:
        return "quarterly"
    if b in ("8-K", "6-K"):
        return "current_report"
    if b in ("3", "4", "5"):
        return "insider"
    if b.startswith("13F") or b.startswith("SC 13") or b.startswith("SCHEDULE 13"):
        return "holdings"
    if "14A" in b or "14C" in b:
        return "proxy"
    if b.startswith(("S-", "F-", "424B")):
        return "prospectus"
    return "other"


def filing_index_url(cik: str, acc: str) -> str:
    return f"{ARCHIVES}/data/{int(cik)}/{acc_nodash(acc)}/{acc}-index.htm"


def doc_url(cik: str, acc: str, doc: str) -> str:
    return f"{ARCHIVES}/data/{int(cik)}/{acc_nodash(acc)}/{doc}"


def quarter_of(d: date) -> int:
    return (d.month - 1) // 3 + 1


def to_date(x: Any) -> date | None:
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    s = str(x).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_since(since: Any) -> date | None:
    if since is None:
        return None
    if isinstance(since, datetime):
        return since.date()
    if isinstance(since, date):
        return since
    return to_date(str(since)[:10])


def parse_acceptance_utc(s: str | None, form: str | None = None, filing_date: Any = None, fetched_on: Any = None,
                         truth: datetime | None = None, file_late: bool | None = None) -> datetime | None:
    """submissions acceptanceDateTime -> aware UTC datetime, CORRECTED for EDGAR's offset bug (settled records are late
    by the US/Eastern offset; see app.ingestion.sec_acceptance). Pass the form, the filing date and the day the record
    was fetched for the best guess; a known true time (`truth`, e.g. from the Atom feed) always wins."""
    from app.ingestion.sec_acceptance import parse_acceptance_utc as _fix  # noqa: PLC0415

    return _fix(s, form, filing_date, fetched_on, truth, file_late)


def parse_header_acceptance(s: str | None) -> datetime | None:
    """SGML header ACCEPTANCE-DATETIME (YYYYMMDDhhmmss, US/Eastern) -> aware UTC datetime."""
    if not s or len(s.strip()) < 14:
        return None
    try:
        return datetime.strptime(s.strip()[:14], "%Y%m%d%H%M%S").replace(tzinfo=ET).astimezone(UTC)
    except ValueError:
        return None


_TRUTH: dict[str, Any] = {"at": 0.0, "map": {}}


def live_truth(settings: Settings | None = None, ttl_s: float = 1800.0) -> dict[str, datetime]:
    """accession -> true acceptance time seen by the real-time EDGAR Atom stream (bus + live_filings), cached."""
    import time as _t  # noqa: PLC0415

    if _t.time() - _TRUTH["at"] > ttl_s:
        try:
            from app.ingestion.sec_acceptance import load_truth  # noqa: PLC0415

            _TRUTH["map"] = load_truth(settings, include_insider=False)
        except Exception:  # noqa: BLE001 - the rule alone still applies
            _TRUTH["map"] = {}
        _TRUTH["at"] = _t.time()
    return _TRUTH["map"]


def region_for_country(country: str | None) -> str:
    if not country:
        return "US"
    return COUNTRY_REGION.get(country.strip(), "OTHER")


def try_lookup_many(id_type: str, values: Iterable[str]) -> dict[str, dict]:
    """Resolve via ref-identity's app.normalize.identity.lookup_many if present; {} otherwise (never raises)."""
    vals = [v for v in dict.fromkeys(values) if v]
    if not vals:
        return {}
    try:
        from app.normalize import identity  # noqa: PLC0415 (optional dependency on a parallel agent)

        df = identity.lookup_many(id_type, vals)
        if df.is_empty():
            return {}
        return {r["input_value"]: r for r in df.to_dicts()}
    except Exception as e:  # pragma: no cover - identity layer not ready / empty
        log.debug("identity lookup unavailable (%s): %s", id_type, e)
        return {}


# ----------------------------------------------------------------------------------------------- base adapter


class SecAdapter(SourceAdapter):
    """Shared base of all sec_* adapters. Not registered itself.

    `source_id` (class attr) is the adapter/registry id; `data_source_id` is the value written into the rows and the
    raw-zone folder (`data/raw/sec_edgar/...`). HTTP goes through one shared 'sec_edgar' limiter.
    """

    data_source_id: str = SEC_SOURCE
    regions = ("US",)

    def __init__(self, settings: Settings | None = None, http: HttpClient | None = None):
        s = settings or get_settings()
        super().__init__(settings=s, http=http or HttpClient(SEC_SOURCE, rate_limit=SEC_RATE, settings=s))
        self._ticker_map: pl.DataFrame | None = None

    # raw zone under data/raw/sec_edgar/<dataset>/dt=YYYY-MM-DD/
    def raw_dir(self, dataset: str, day: date | None = None) -> Path:
        d = day or datetime.now(UTC).date()
        p = self.settings.raw_dir / self.data_source_id / dataset / f"dt={d.isoformat()}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---- state (per-adapter cursor file) --------------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.settings.data_dir / "state" / f"{self.source_id}.json"

    def load_state(self) -> dict[str, Any]:
        p = self.state_path
        if p.exists():
            try:
                return orjson.loads(p.read_bytes())
            except orjson.JSONDecodeError:
                return {}
        return {}

    def save_state(self, **kv: Any) -> None:
        st = self.load_state()
        st.update(kv)
        st["updated_at"] = datetime.now(UTC).isoformat()
        p = self.state_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(orjson.dumps(st, option=orjson.OPT_INDENT_2, default=str))
        tmp.replace(p)

    # ---- reference: tickers <-> CIK ---------------------------------------------------------------------------
    def ticker_map(self) -> pl.DataFrame:
        """SEC company_tickers_exchange.json as a frame (cik, name, ticker, exchange, rank). rank ~ market-cap order."""
        if self._ticker_map is None:
            r = self.http.get(f"{SEC_WWW}/files/company_tickers_exchange.json", cache_ttl=24 * 3600)
            raw_path = self.raw_dir("company_tickers") / "company_tickers_exchange.json.gz"
            if not raw_path.exists():
                self.save_raw("company_tickers", "company_tickers_exchange", r.content, day=None)
            doc = r.json()
            rows = [dict(zip(doc["fields"], rec, strict=False)) for rec in doc["data"]]
            df = pl.DataFrame(rows, schema={"cik": pl.Int64, "name": pl.Utf8, "ticker": pl.Utf8, "exchange": pl.Utf8},
                              orient="row") if rows else pl.DataFrame()
            df = df.with_row_index("rank").with_columns(
                pl.col("cik").cast(pl.Utf8).str.zfill(10).alias("cik"),
                (pl.col("rank") + 1).cast(pl.Int32),
            )
            self._ticker_map = df
        return self._ticker_map

    def discover(self) -> pl.DataFrame:
        tm = self.ticker_map()
        rows = [
            {"source_entity_id": r["cik"], "name": r["name"], "cik": r["cik"], "ticker": r["ticker"],
             "mic": EXCHANGE_MIC.get(r["exchange"] or ""), "country": None, "is_active": True}
            for r in tm.to_dicts()
        ]
        return self.discover_frame(rows, self.data_source_id)

    # ---- submissions API ------------------------------------------------------------------------------------
    def submissions(self, cik: str, ttl: int = 6 * 3600, land: bool = True) -> dict | None:
        try:
            r = self.http.get(f"{SEC_DATA}/submissions/CIK{cik10(cik)}.json", cache_ttl=ttl)
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        if land and not r.from_cache:
            self.save_raw("submissions", f"CIK{cik10(cik)}", r.content)
        return r.json()

    def issuer_meta(self, ciks: Iterable[str]) -> dict[str, dict]:
        """cik10 -> {name, country, region, fiscal_year_end, sic, tickers}. Cached for 7 days per CIK (HTTP cache),
        mirrored in data/state/sec_issuer_meta.json so later runs need no request."""
        path = self.settings.data_dir / "state" / "sec_issuer_meta.json"
        meta: dict[str, dict] = {}
        if path.exists():
            try:
                meta = orjson.loads(path.read_bytes())
            except orjson.JSONDecodeError:
                meta = {}
        changed = False
        for c in {cik10(x) for x in ciks}:
            if c in meta:
                continue
            try:
                s = self.submissions(c, ttl=7 * 24 * 3600)
            except Exception as e:  # noqa: BLE001
                log.warning("submissions %s failed: %s", c, e)
                continue
            if not s:
                continue
            meta[c] = submissions_meta(s)
            changed = True
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(orjson.dumps(meta))
        return meta

    # ---- universe ---------------------------------------------------------------------------------------------
    def universe_ciks(self, params: dict[str, Any] | None = None) -> list[str]:
        return resolve_universe(self, params or {})


def submissions_meta(s: dict) -> dict:
    addr = (s.get("addresses") or {}).get("business") or {}
    foreign = bool(addr.get("isForeignLocation"))
    country = addr.get("country") if foreign else "United States"
    if foreign and not country:
        country = s.get("stateOfIncorporationDescription")
    return {
        "name": s.get("name"),
        "country": country,
        "region": "US" if not foreign else region_for_country(country),
        "fiscal_year_end": s.get("fiscalYearEnd"),
        "sic": s.get("sic"),
        "tickers": s.get("tickers") or [],
        "exchanges": s.get("exchanges") or [],
        "lei": s.get("lei"),
    }


def resolve_universe(ad: SecAdapter, params: dict[str, Any]) -> list[str]:
    """Universe CIKs, in priority: params.ciks / params.tickers > universe_membership (via identifier_map cik) >
    data/reference/seeds/mvp_us.csv (cik column) > fallback (top-N Nasdaq/NYSE names of company_tickers_exchange,
    which SEC orders by market cap, + ADR_EXTRAS)."""
    if params.get("ciks"):
        return sorted({cik10(c) for c in params["ciks"]})
    tm = None
    if params.get("tickers"):
        tm = ad.ticker_map()
        sel = tm.filter(pl.col("ticker").is_in([t.upper() for t in params["tickers"]]))
        return sorted(set(sel["cik"].to_list()))
    s = ad.settings
    try:
        um = read_table("universe_membership", settings=s)
        if um.height:
            cur = um.filter(pl.col("valid_to").is_null())
            secs = set(cur["security_id"].to_list())
            sm = read_table("securities_master", settings=s)
            issuers = set(sm.filter(pl.col("security_id").is_in(list(secs)))["issuer_id"].to_list()) if sm.height else set()
            idm = read_table("identifier_map", settings=s).filter(pl.col("id_type") == "cik")
            ciks = idm.filter(pl.col("issuer_id").is_in(list(issuers)) | pl.col("security_id").is_in(list(secs)))
            out = sorted({cik10(v) for v in ciks["id_value"].to_list()})
            if out:
                return out
    except Exception as e:  # noqa: BLE001 - reference tables not built yet
        log.debug("universe_membership not usable yet: %s", e)
    seed = s.data_dir / "reference" / "seeds" / "mvp_us.csv"
    if seed.exists():
        try:
            with seed.open(encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            ciks = {cik10(r["cik"]) for r in rows if r.get("cik")}
            if ciks:
                return sorted(ciks)
        except Exception as e:  # noqa: BLE001
            log.debug("seed mvp_us.csv not usable: %s", e)
    n = int(params.get("fallback_top_n", 50))
    tm = tm if tm is not None else ad.ticker_map()
    top = tm.filter(pl.col("exchange").is_in(["Nasdaq", "NYSE"])).unique("cik", keep="first", maintain_order=True).head(n)
    extras = tm.filter(pl.col("ticker").is_in(list(ADR_EXTRAS)))
    return sorted(set(top["cik"].to_list()) | set(extras["cik"].to_list()))


# ----------------------------------------------------------------------------------------------- index parsing


def parse_master_idx(text: str, index_date: date | None = None) -> list[dict[str, Any]]:
    """Parse master.idx (daily or full): 'CIK|Company Name|Form Type|Date Filed|File Name' after the dashes line.
    index_date = date of the daily index file (dissemination date), None for full-index files."""
    out = []
    started = False
    for line in text.splitlines():
        if not started:
            if line.startswith("-----"):
                started = True
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        if len(parts) > 5:  # '|' inside the company name
            parts = [parts[0], "|".join(parts[1:-3]), parts[-3], parts[-2], parts[-1]]
        cik, name, form, filed, fname = (p.strip() for p in parts)
        if not cik.isdigit():
            continue
        m = re.search(r"(\d{10}-\d{2}-\d{6})", fname)
        if not m:
            continue
        out.append({"cik": cik10(cik), "name": name, "form": form, "filed": filed, "accession": m.group(1),
                    "index_date": index_date})
    return out


def build_index_rows(entries: list[dict[str, str]], known_issuers: set[str], subs: dict[str, dict],
                     all_forms: bool = False, now: datetime | None = None) -> pl.DataFrame:
    """Group index lines by accession -> one filings_index row. For multi-CIK filings (Forms 3/4/5 joint filings,
    13D/G) the source_entity_id prefers a CIK that has a ticker (issuer) over a reporting owner.
    `subs` = accession -> submissions record (acceptanceDateTime, reportDate, primaryDocument, items, isXBRL)."""
    now = now or datetime.now(UTC)
    by_acc: dict[str, list[dict[str, str]]] = {}
    for e in entries:
        if not all_forms and base_form(e["form"]) not in INDEX_FORMS:
            continue
        by_acc.setdefault(e["accession"], []).append(e)
    rows = []
    for acc, es in by_acc.items():
        pick = next((e for e in es if e["cik"] in known_issuers), es[0])
        filed = to_date(pick["filed"])
        if filed is None:
            continue
        # delayed dissemination (old 'Date Filed' first appearing in a recent daily index): the filing only became
        # public on the index date -> PIT-conservative knowledge_date
        idx_day = pick.get("index_date")
        kd = idx_day if idx_day is not None and (idx_day - filed).days > DISSEMINATION_LAG_DAYS else filed
        sub = subs.get(acc) or {}
        form = sub.get("form") or pick["form"]
        b = base_form(form)
        cik = pick["cik"]
        primary = sub.get("primaryDocument")
        items = [i.strip() for i in (sub.get("items") or "").split(",") if i.strip()] or None
        has_xbrl = bool(sub.get("isXBRL")) if "isXBRL" in sub else (b in XBRL_FORMS)
        rows.append({
            "filing_id": make_filing_id(acc),
            "source_id": SEC_SOURCE,
            "year": kd.year,
            "source_entity_id": cik,
            "issuer_id": None,
            "form_type": form,
            "form_class": form_class(form),
            "period_end": to_date(sub.get("reportDate")),
            "fiscal_year": None,
            "fiscal_period": None,
            "knowledge_date": kd,
            "knowledge_ts": parse_acceptance_utc(sub.get("acceptanceDateTime"), form, sub.get("filingDate") or filed,
                                                 now.date(), live_truth().get(acc), sub.get("_file_late")),
            "is_amendment": is_amendment(form),
            "amends_filing_id": None,
            "language": "en",
            "url": doc_url(cik, acc, primary) if primary else filing_index_url(cik, acc),
            "doc_path": None,
            "has_xbrl": has_xbrl,
            "items": items,
            "ingested_at": now,
        })
    return filings_frame(rows)


FILINGS_SCHEMA = {
    "filing_id": pl.Utf8, "source_id": pl.Utf8, "year": pl.Int16, "source_entity_id": pl.Utf8, "issuer_id": pl.Utf8,
    "form_type": pl.Utf8, "form_class": pl.Utf8, "period_end": pl.Date, "fiscal_year": pl.Int16,
    "fiscal_period": pl.Utf8, "knowledge_date": pl.Date, "knowledge_ts": pl.Datetime("us", "UTC"),
    "is_amendment": pl.Boolean, "amends_filing_id": pl.Utf8, "language": pl.Utf8, "url": pl.Utf8,
    "doc_path": pl.Utf8, "has_xbrl": pl.Boolean, "items": pl.List(pl.Utf8), "ingested_at": pl.Datetime("us", "UTC"),
}


def filings_frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=FILINGS_SCHEMA, orient="row") if rows else pl.DataFrame(schema=FILINGS_SCHEMA)


def submissions_recent_by_acc(s: dict) -> dict[str, dict]:
    """submissions JSON -> accession -> record (columnar 'recent' block transposed). Each record carries `_file_late`:
    whether this file serves its acceptance times late by the ET offset (app.ingestion.sec_acceptance.block_vote)."""
    from app.ingestion.sec_acceptance import block_vote  # noqa: PLC0415

    rec = (s.get("filings") or {}).get("recent") or {}
    accs = rec.get("accessionNumber") or []
    keys = list(rec.keys())
    late = block_vote(rec)
    return {a: {**{k: rec[k][i] for k in keys if i < len(rec[k])}, "_file_late": late} for i, a in enumerate(accs)}


def read_existing_filings(settings: Settings, filing_ids: Sequence[str] | None = None,
                          filters: pl.Expr | None = None) -> pl.DataFrame:
    lf = scan_table("filings_index", settings).filter(pl.col("source_id") == SEC_SOURCE)
    if filing_ids is not None:
        lf = lf.filter(pl.col("filing_id").is_in(list(filing_ids)))
    if filters is not None:
        lf = lf.filter(filters)
    return lf.collect()


# ----------------------------------------------------------------------------------------------- adapter


@register_adapter
class SecIndexAdapter(SecAdapter):
    source_id = "sec_index"
    produces = ("filings_index",)
    cache_ttl = -1

    def __init__(self, *a: Any, params: dict[str, Any] | None = None, **kw: Any):
        super().__init__(*a, **kw)
        self.params = params or {}

    # -- which index files ---------------------------------------------------------------------------------------
    def _daily_listing(self, year: int, q: int) -> dict[date, str]:
        url = f"{ARCHIVES}/daily-index/{year}/QTR{q}/index.json"
        try:
            items = self.http.get(url, cache_ttl=3600).json()["directory"]["item"]
        except HttpError as e:
            if e.status in (403, 404):
                return {}
            raise
        out = {}
        for it in items:
            m = re.match(r"master\.(\d{8})\.idx$", it.get("name", ""))
            if m:
                out[datetime.strptime(m.group(1), "%Y%m%d").date()] = it["name"]
        return out

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(source_id=SEC_SOURCE, started_at=datetime.now(UTC))
        as_of = parse_since(self.params.get("as_of")) or datetime.now(ET).date()
        start = parse_since(since)
        if start is None:
            lookback = int(self.params.get("lookback_days", 30))
            start = as_of - timedelta(days=lookback + 1)
        full_quarters: list[tuple[int, int]] = []
        if self.params.get("use_full_index"):
            # backfill whole quarters strictly before the current one from full-index (1 request per quarter)
            y, q = start.year, quarter_of(start)
            cy, cq = as_of.year, quarter_of(as_of)
            while (y, q) < (cy, cq):
                full_quarters.append((y, q))
                y, q = (y + 1, 1) if q == 4 else (y, q + 1)
            if full_quarters:
                ly, lq = full_quarters[-1]
                start = date(ly + (lq == 4), 1 if lq == 4 else lq * 3 + 1, 1) - timedelta(days=1)
        entries: list[dict[str, str]] = []
        for (y, q) in full_quarters:
            url = f"{ARCHIVES}/full-index/{y}/QTR{q}/master.idx"
            try:
                r = self.http.get(url, cache_ttl=24 * 3600)
                p = self.save_raw("full_index", f"master_{y}Q{q}.idx", r.content, day=date(y, (q - 1) * 3 + 1, 1))
                res.raw_paths.append(p)
                entries.extend(parse_master_idx(self.load_raw(p).decode("latin-1"), None))
                res.entities_ok.append(f"{y}Q{q}")
            except Exception as e:  # noqa: BLE001
                res.entities_failed[f"{y}Q{q}"] = str(e)[:300]
        # daily files in (start, as_of]
        days: dict[date, str] = {}
        d = start
        seen_q = set()
        while d <= as_of:
            key = (d.year, quarter_of(d))
            if key not in seen_q:
                seen_q.add(key)
                days.update(self._daily_listing(*key))
            d += timedelta(days=1)
        wm = start
        gap = False  # the watermark only advances over a contiguous run of successfully fetched days
        for day in sorted(k for k in days if start < k <= as_of):
            url = f"{ARCHIVES}/daily-index/{day.year}/QTR{quarter_of(day)}/{days[day]}"
            try:
                r = self.http.get(url, cache_ttl=-1 if day < as_of else 3600)
                p = self.save_raw("daily_index", "master.idx", r.content, day=day)
                res.raw_paths.append(p)
                entries.extend(parse_master_idx(self.load_raw(p).decode("latin-1"), day))
                res.entities_ok.append(day.isoformat())
                if not gap:
                    wm = max(wm, day)
            except Exception as e:  # noqa: BLE001
                gap = True
                res.entities_failed[day.isoformat()] = str(e)[:300]
        res.watermark = wm.isoformat()
        res.meta["n_index_lines"] = len(entries)
        # enrich universe CIKs that filed with the submissions API
        universe = set(entities) if entities else set(self.universe_ciks(self.params))
        filed_ciks = {e["cik"] for e in entries} & universe
        subs: dict[str, dict] = {}
        for c in sorted(filed_ciks):
            try:
                s = self.submissions(c, ttl=1800)
                if s:
                    subs.update(submissions_recent_by_acc(s))
            except Exception as e:  # noqa: BLE001
                res.entities_failed[f"submissions:{c}"] = str(e)[:300]
        res.meta["entries"] = entries
        res.meta["subs"] = subs
        res.meta["n_enriched_ciks"] = len(filed_ciks)
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        entries = fetched.meta.get("entries") or []
        known = set(self.ticker_map()["cik"].to_list())
        df = build_index_rows(entries, known, fetched.meta.get("subs") or {},
                              all_forms=bool(self.params.get("all_forms")))
        if df.height:
            # keep doc_path / richer metadata written earlier by sec_text / sec_form4 (upsert would drop them)
            prev = read_existing_filings(self.settings, df["filing_id"].to_list())
            if prev.height:
                df = merge_preserving(df, prev)
        return {"filings_index": df}


def merge_preserving(new: pl.DataFrame, prev: pl.DataFrame) -> pl.DataFrame:
    """For rows already in filings_index: keep non-null previous values where the new row has nulls."""
    keep_cols = ["period_end", "knowledge_ts", "doc_path", "items", "fiscal_year", "fiscal_period", "amends_filing_id"]
    p = prev.select(["filing_id"] + keep_cols).rename({c: f"__p_{c}" for c in keep_cols})
    j = new.join(p, on="filing_id", how="left")
    j = j.with_columns([pl.coalesce(pl.col(c), pl.col(f"__p_{c}")).alias(c) for c in keep_cols])
    # source_entity_id: Form 3/4/5 rows corrected to the issuer CIK by sec_form4 must not flip back
    if "source_entity_id" in prev.columns:
        j = j.join(prev.select("filing_id", pl.col("source_entity_id").alias("__p_sei")), on="filing_id", how="left")
        j = j.with_columns(pl.coalesce(pl.col("__p_sei"), pl.col("source_entity_id")).alias("source_entity_id"))
    return j.select(new.columns)


# ----------------------------------------------------------------------------------------------- job entry


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    ad = SecIndexAdapter(settings=ctx.settings, params={**ctx.params, "as_of": ctx.params.get("as_of") or ctx.as_of})
    since = ctx.params.get("since") or ctx.watermark or ad.load_state().get("watermark")
    fetched, rows = ad.run(since=since, entities=ctx.params.get("ciks"))
    if fetched.status != "failed" and fetched.watermark:
        ad.save_state(watermark=fetched.watermark)
    return JobResult(status=fetched.status, rows_written=rows, entities_ok=len(fetched.entities_ok),
                     entities_failed=len(fetched.entities_failed), watermark=fetched.watermark,
                     http_requests=fetched.n_requests, cache_hits=fetched.n_cache_hits,
                     message=f"index_lines={fetched.meta.get('n_index_lines')} enriched_ciks={fetched.meta.get('n_enriched_ciks')}")
