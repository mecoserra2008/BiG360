"""Shared helpers for the ingest-global adapters (ESEF, FCA NSM, EU PDMR registers, EDINET, TDnet, OpenDART, CVM,
BaoStock, AKShare, HKEX, Companies House, yfinance fundamentals).

Owner: ingest-global. Nothing here is imported by other agents' code.

Contents
  * row builders for filings_index / fundamentals_raw / insider_transactions (contract column names)
  * fiscal-year + imputed knowledge-date rules (docs/ARCHITECTURE.md §7-8)
  * number / date parsing for German, Brazilian, Japanese, Korean formats
  * per-source JSON state (data/state/<source_id>.json) for watermarks and quotas
  * HardTimeoutRunner: runs fragile library calls (AKShare, BaoStock) in a killable child process
  * universe helpers reading data/reference/seeds/mvp_global.csv (read-only)
  * run_adapter_job(): job-module entry used by every `run(ctx)` in the global adapters
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import re
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.ingestion.global")

# --------------------------------------------------------------------------------------------- regions / GAAP

EU_COUNTRIES = {"AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IS",
                "IT", "LI", "LT", "LU", "LV", "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK"}
LATAM_COUNTRIES = {"AR", "BR", "CL", "CO", "MX", "PE", "UY"}


def region_of_country(cc: str | None) -> str:
    cc = (cc or "").upper()
    if cc == "US":
        return "US"
    if cc in ("GB", "UK", "JE", "GG", "IM"):
        return "UK"
    if cc in EU_COUNTRIES:
        return "EU"
    if cc in ("JP", "KR", "CN", "HK", "CA"):
        return cc
    if cc in LATAM_COUNTRIES:
        return "LATAM"
    if cc in ("AU", "NZ", "SG", "TW", "IN", "ID", "MY", "TH", "PH", "VN"):
        return "APAC"
    return "OTHER"


#: accounting standard labels used in fundamentals_raw.label suffixes / docs (the taxonomy enum carries the namespace)
ACCOUNTING_STANDARDS = ("IFRS", "J-GAAP", "JMIS", "US-GAAP", "K-IFRS", "CAS", "BR-IFRS", "HKFRS", "UK-GAAP")

# --------------------------------------------------------------------------------------------- dates


def to_date(v: Any) -> date | None:
    """Parse common date spellings: ISO, YYYYMMDD, dd.mm.yyyy, dd/mm/yyyy (day-first), datetime objects."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "nat", "-", "－"):
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:  # day-first (DE, BR, HK, UK)
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def to_date_us(s: str | None) -> date | None:
    """Month-first dates like AFM '10/8/2026 12:00:00 AM'."""
    if not s:
        return None
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s.strip())
    return date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS.update({"sept": 9, "okt": 10, "mrt": 3, "mei": 5})


def parse_text_date(s: str | None) -> date | None:
    """'9 October 2026', '09-Oct-2026', 'October 9, 2026', '2026-10-09', '09/10/2026' (day-first)."""
    if not s:
        return None
    s = s.strip()
    d = to_date(s)
    if d:
        return d
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?,?[\s\-]+(\d{4})", s)
    if m and m.group(2).lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def utcnow() -> datetime:
    return datetime.now(UTC)


def jst_to_utc(d: date, hhmm: str | None) -> datetime | None:
    """Japanese/Korean local time (UTC+9) -> UTC datetime."""
    if not hhmm:
        return None
    m = re.match(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m:
        return None
    return datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2)), tzinfo=UTC) - timedelta(hours=9)


def hkt_to_utc(d: date, hhmm: str | None) -> datetime | None:
    """Hong Kong / China time (UTC+8) -> UTC."""
    if not hhmm:
        return None
    m = re.match(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m:
        return None
    return datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2)), tzinfo=UTC) - timedelta(hours=8)


def fiscal_year_for(period_end: date, fye_month: int | None = None, fye_day: int | None = None) -> int:
    """fiscal_year = calendar year in which the fiscal year containing `period_end` ENDS (ARCHITECTURE §8)."""
    if not fye_month or fye_month == 12:
        return period_end.year
    # a period ending in or before the FY-end month belongs to the FY ending this calendar year (Toyota Jun-2025 -> 2026)
    return period_end.year if period_end.month <= fye_month else period_end.year + 1


#: imputed knowledge-date lags (days after period end), ARCHITECTURE §7
LAG_DAYS = {("US", "annual"): 75, ("US", "quarterly"): 45, ("nonUS", "annual"): 120, ("nonUS", "quarterly"): 60}


def impute_knowledge_date(period_end: date, annual: bool, region: str, first_seen: date | None = None) -> date:
    """period_end + lag (US 75/45 d, non-US 120/60 d), capped at the first-seen date (we observed it then)."""
    key = ("US" if region == "US" else "nonUS", "annual" if annual else "quarterly")
    kd = period_end + timedelta(days=LAG_DAYS[key])
    if first_seen is not None and kd > first_seen:
        kd = first_seen
    return max(kd, period_end)


def period_start_for(end: date, months: int) -> date:
    """First day of the `months`-month period ending at month-end `end` (Jun-30, 3 -> Apr-01; Dec-31, 12 -> Jan-01)."""
    m = end.month - months + 1
    y = end.year
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + n, day=28)


# --------------------------------------------------------------------------------------------- numbers


def parse_num(v: Any, decimal: str = ".") -> float | None:
    """Parse '1.234,56' (decimal=','), '1,234.56', '(123)' negatives, '△123' (JP negative), '-' -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f
    s = str(v).strip().replace(" ", "").replace(" ", "")
    if not s or s in ("-", "－", "—", "–", "nan", "NaN", "None", "N/A", "n/a"):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s[:1] in ("△", "▲"):
        neg, s = True, s[1:]
    s = re.sub(r"[A-Za-z]{2,}", "", s)  # currency codes / units ('EUR', 'KRW')
    s = re.sub(r"[^\d,.\-+eE]", "", s)
    if not s:
        return None
    if decimal == ",":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


# --------------------------------------------------------------------------------------------- dimensions


def dimensions_hash(dims: dict[str, str] | None) -> str:
    if not dims:
        return ""
    s = "|".join(f"{k}={v}" for k, v in sorted(dims.items()))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def dimensions_json(dims: dict[str, str] | None) -> str | None:
    if not dims:
        return None
    return orjson.dumps(dict(sorted(dims.items()))).decode()


def norm_person(name: str | None) -> str:
    """Normalized person/entity key for insider_id."""
    s = re.sub(r"\s+", " ", (name or "").strip()).lower()
    s = re.sub(r"^(mr|mrs|ms|dr|prof|sir|dame|lord)\.?\s+", "", s)
    return re.sub(r"[^\w ,.\-]", "", s)


# --------------------------------------------------------------------------------------------- row builders


def filing_row(*, source_id: str, native_key: str, source_entity_id: str, form_type: str, form_class: str,
               knowledge_date: date, knowledge_ts: datetime | None = None, period_end: date | None = None,
               fiscal_year: int | None = None, fiscal_period: str | None = None, is_amendment: bool = False,
               amends_filing_id: str | None = None, language: str | None = None, url: str | None = None,
               doc_path: str | None = None, has_xbrl: bool = False, items: list[str] | None = None) -> dict[str, Any]:
    return {
        "filing_id": f"{source_id}:{native_key}", "source_id": source_id, "year": knowledge_date.year,
        "source_entity_id": source_entity_id, "issuer_id": None, "form_type": form_type, "form_class": form_class,
        "period_end": period_end, "fiscal_year": fiscal_year, "fiscal_period": fiscal_period,
        "knowledge_date": knowledge_date, "knowledge_ts": knowledge_ts, "is_amendment": is_amendment,
        "amends_filing_id": amends_filing_id, "language": language, "url": url, "doc_path": doc_path,
        "has_xbrl": has_xbrl, "items": items, "ingested_at": utcnow(),
    }


def fact_row(*, source_id: str, source_entity_id: str, filing_id: str, form_type: str | None, taxonomy: str,
             concept: str, label: str | None, period_start: date | None, period_end: date, value: float, unit: str,
             knowledge_date: date, fiscal_year: int | None = None, fiscal_period: str | None = None,
             decimals: int | None = None, dims: dict[str, str] | None = None, consolidated: bool | None = True,
             knowledge_date_estimated: bool = False) -> dict[str, Any]:
    return {
        "source_id": source_id, "fiscal_year": fiscal_year if fiscal_year is not None else period_end.year,
        "source_entity_id": source_entity_id, "issuer_id": None, "filing_id": filing_id, "form_type": form_type,
        "taxonomy": taxonomy, "concept": concept, "label": label,
        "period_type": "instant" if period_start is None else "duration", "period_start": period_start,
        "period_end": period_end, "fiscal_period": fiscal_period, "value": float(value), "unit": unit,
        "decimals": decimals, "dimensions_hash": dimensions_hash(dims), "dimensions_json": dimensions_json(dims),
        "consolidated": consolidated, "knowledge_date": knowledge_date,
        "knowledge_date_estimated": knowledge_date_estimated, "ingested_at": utcnow(),
    }


ROLE_PATTERNS: list[tuple[str, str]] = [
    (r"\b(chief executive|ceo|managing director|vorstandsvorsitz|présidente? directeur général|pdg|代表取締役社長|대표이사)\b", "ceo"),
    (r"\b(chief financial|cfo|finance director|finanzvorstand|financieel directeur)\b", "cfo"),
    (r"\b(chief operating|coo)\b", "coo"),
    (r"\b(chair|chairman|chairwoman|vorsitzender des aufsichtsrat|president of the board|presidente do conselho|会長|회장)\b", "chair"),
    (r"\b(president)\b", "president"),
    (r"\b(director|aufsichtsrat|supervisory board|board member|non-executive|ned|conselho de administra|bestuurder|commissaris|이사|取締役)", "director"),
    (r"\b(officer|vorstand|executive|manag|diretor|directeur|pdmr|임원|executive board)", "officer_other"),
    (r"(controlador|substantial shareholder|major shareholder|최대주주|주요주주|10%|5%)", "owner"),
]


def norm_role(title: str | None) -> str | None:
    if not title:
        return None
    t = title.lower()
    for pat, role in ROLE_PATTERNS:
        if re.search(pat, t):
            return role
    return "other"


def insider_row(*, source_id: str, filing_id: str, line: int | str, region: str, source_entity_id: str,
                insider_name: str, transaction_date: date, knowledge_date: date, txn_type: str, shares: float,
                is_open_market: bool, officer_title: str | None = None, is_director: bool | None = None,
                is_officer: bool | None = None, is_ten_pct_owner: bool = False, role_norm: str | None = None,
                knowledge_ts: datetime | None = None, txn_code: str | None = None, is_derivative: bool = False,
                price: float | None = None, currency: str | None = None, shares_owned_after: float | None = None,
                ownership_nature: str | None = None, is_amendment: bool = False,
                insider_key: str | None = None, form_type: str | None = None, security_title: str | None = None,
                acquired_disposed: str | None = None) -> dict[str, Any]:
    role = role_norm or norm_role(officer_title)
    if is_director is None:
        is_director = role in ("director", "chair")
    if is_officer is None:
        is_officer = role in ("ceo", "cfo", "coo", "president", "officer_other")
    shares = abs(float(shares))
    value_local = shares * price if price is not None else None
    if acquired_disposed is None:
        acquired_disposed = ("A" if txn_type in ("buy", "grant", "option_exercise") else
                             "D" if txn_type in ("sell", "gift", "tax_withholding") else None)
    change = None
    if shares_owned_after is not None and shares and acquired_disposed:
        before = shares_owned_after - shares if acquired_disposed == "A" else shares_owned_after + shares
        change = (shares / before if acquired_disposed == "A" else -shares / before) if before else None
    return {
        "txn_id": f"{filing_id}:{line}", "region": region, "year": knowledge_date.year, "filing_id": filing_id,
        "source_id": source_id, "source_entity_id": source_entity_id, "issuer_id": None, "security_id": None,
        "insider_id": f"{source_id}:{insider_key or norm_person(insider_name)}", "insider_name": insider_name.strip(),
        "is_director": bool(is_director), "is_officer": bool(is_officer), "is_ten_pct_owner": bool(is_ten_pct_owner),
        "officer_title": officer_title, "role_norm": role, "transaction_date": transaction_date,
        "knowledge_date": knowledge_date, "knowledge_ts": knowledge_ts, "txn_code": txn_code, "txn_type": txn_type,
        "is_open_market": bool(is_open_market), "is_derivative": bool(is_derivative), "is_10b5_1": None,
        "shares": shares, "price": price, "currency": currency, "value_local": value_local, "value_usd": None,
        "shares_owned_after": shares_owned_after, "holding_change_pct": change, "ownership_nature": ownership_nature,
        "is_amendment": bool(is_amendment), "ingested_at": utcnow(), "form_type": form_type,
        "security_title": security_title, "acquired_disposed": acquired_disposed,
    }


def frame(rows: list[dict[str, Any]], table: str) -> pl.DataFrame:
    """Rows -> DataFrame with the contract schema (nullable columns padded); dedupe on PK keeping the first row."""
    from app.core.schema import conform, get_contract

    c = get_contract(table)
    schema = c.polars_schema()
    if not rows:
        return pl.DataFrame(schema=schema)
    df = pl.DataFrame(rows, schema={k: v for k, v in schema.items() if k in rows[0]}, strict=False)
    if c.primary_key:
        df = df.unique(subset=list(c.primary_key), keep="first", maintain_order=True)
    return conform(df, table)


# --------------------------------------------------------------------------------------------- state


def state_path(source_id: str, settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    p = s.data_dir / "state" / f"{source_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_state(source_id: str, settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(source_id, settings)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_state(source_id: str, state: dict[str, Any], settings: Settings | None = None) -> None:
    p = state_path(source_id, settings)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


class DailyQuota:
    """Persisted per-day call counter (e.g. OpenDART 20,000 calls/day). Thread-safe within the process."""

    def __init__(self, source_id: str, limit: int, settings: Settings | None = None, reserve: int = 500):
        self.source_id, self.limit, self.reserve = source_id, limit, reserve
        self.settings = settings
        self._lock = threading.Lock()

    def _today(self) -> str:
        # DART resets at midnight KST
        return (datetime.now(UTC) + timedelta(hours=9)).date().isoformat()

    def used(self) -> int:
        st = load_state(f"{self.source_id}_quota", self.settings)
        return int(st.get(self._today(), 0))

    def take(self, n: int = 1) -> bool:
        with self._lock:
            st = load_state(f"{self.source_id}_quota", self.settings)
            day = self._today()
            used = int(st.get(day, 0))
            if used + n > self.limit - self.reserve:
                return False
            st = {day: used + n}  # keep only today
            save_state(f"{self.source_id}_quota", st, self.settings)
            return True


# --------------------------------------------------------------------------------------------- hard timeouts


def _child_call(target: str, kwargs: dict[str, Any]) -> Any:
    """Executed in the worker process: import 'module:function' and call it."""
    mod_name, fn_name = target.split(":")
    import importlib

    fn = getattr(importlib.import_module(mod_name), fn_name)
    return fn(**kwargs)


class CallTimeout(TimeoutError):
    pass


class HardTimeoutRunner:
    """Run fragile library calls in a single persistent child process; on timeout the process is killed and a new one
    is spawned for the next call. Targets are 'package.module:function' strings returning picklable values."""

    def __init__(self, timeout_s: float = 60.0):
        self.timeout_s = timeout_s
        self._pool: Any = None
        self.n_calls = 0
        self.n_timeouts = 0

    def _ensure(self) -> Any:
        if self._pool is None:
            self._pool = mp.get_context("spawn").Pool(processes=1, maxtasksperchild=200)
        return self._pool

    def call(self, target: str, timeout_s: float | None = None, **kwargs: Any) -> Any:
        pool = self._ensure()
        self.n_calls += 1
        res = pool.apply_async(_child_call, (target, kwargs))
        try:
            return res.get(timeout=timeout_s or self.timeout_s)
        except mp.TimeoutError as e:
            self.n_timeouts += 1
            self.kill()
            raise CallTimeout(f"{target} exceeded {timeout_s or self.timeout_s:.0f}s (process killed)") from e

    def kill(self) -> None:
        if self._pool is not None:
            try:
                self._pool.terminate()
                self._pool.join()
            except Exception:  # noqa: BLE001
                pass
            self._pool = None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool.join()
            self._pool = None

    def __enter__(self) -> "HardTimeoutRunner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.kill()


# --------------------------------------------------------------------------------------------- universe


def seed_universe(settings: Settings | None = None, countries: Iterable[str] | None = None,
                  regions: Iterable[str] | None = None) -> pl.DataFrame:
    """Curated global seed (ref-identity owned, read-only here). Empty frame if missing."""
    s = settings or get_settings()
    p = s.data_dir / "reference" / "seeds" / "mvp_global.csv"
    if not p.exists():
        return pl.DataFrame(schema={"ticker": pl.Utf8, "mic": pl.Utf8, "yf_ticker": pl.Utf8, "name": pl.Utf8,
                                    "country": pl.Utf8, "region": pl.Utf8})
    df = pl.read_csv(p, infer_schema_length=0)
    if countries:
        df = df.filter(pl.col("country").is_in(list(countries)))
    if regions:
        df = df.filter(pl.col("region").is_in(list(regions)))
    return df


# --------------------------------------------------------------------------------------------- jobs


def run_adapter_job(adapter_cls: type, ctx: Any, warn_if: Any = None, **run_kwargs: Any) -> Any:
    """Common `run(ctx)` body: instantiate adapter, honour watermark/params, map MissingApiKey -> skipped.
    `warn_if(error: str) -> bool`: per-entity failures it matches are logged as warnings and do not make the job
    partial, as long as at least one entity succeeded (e.g. sporadic BaoStock per-call timeouts)."""
    from app.ingestion.base import MissingApiKey
    from app.scheduler.base import JobResult

    params = dict(getattr(ctx, "params", {}) or {})
    since = params.pop("since", None) or getattr(ctx, "watermark", None)
    entities = params.pop("entities", None)
    if isinstance(entities, str):
        entities = [e.strip() for e in entities.split(",") if e.strip()]
    try:
        adapter = adapter_cls(settings=ctx.settings, **params)
    except TypeError:
        adapter = adapter_cls(settings=ctx.settings)
    t0 = time.time()
    try:
        fetched, rows = adapter.run(since=since, entities=entities, **run_kwargs)
    except MissingApiKey as e:
        return JobResult(status="skipped", message=f"missing key: {e}")
    if warn_if is not None and fetched.entities_ok:
        soft = {k: v for k, v in fetched.entities_failed.items() if warn_if(str(v))}
        for k in soft:
            fetched.entities_failed.pop(k)
        if soft:
            fetched.meta.setdefault("warnings", {}).update(soft)
            log_ = getattr(ctx, "log", None)
            if log_ is not None:
                log_.warning("%d entity failure(s) downgraded to warnings: %s", len(soft), list(soft.items())[:5])
    status = fetched.status
    msg = fetched.meta.get("message") or (f"failed: {list(fetched.entities_failed.items())[:3]}"
                                          if fetched.entities_failed else None)
    return JobResult(status=status, rows_written=rows, entities_ok=len(fetched.entities_ok),
                     entities_failed=len(fetched.entities_failed), watermark=fetched.watermark,
                     http_requests=fetched.n_requests, cache_hits=fetched.n_cache_hits,
                     message=(msg or "") + f" ({time.time() - t0:.0f}s)")


def json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(type(o))


def chunked(seq: list[Any], n: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def safe_call(fn: Callable[[], Any], fetched: Any, entity: str, log_=None) -> Any:
    """Run fn(); on exception record entity failure (message redacted by logger) and return None.

    Circuit breaker (ARCHITECTURE §14): once more than 50% of the first 20 calls failed with the same error class,
    every further call is short-circuited (recorded as failed with 'circuit open') so a blocked endpoint is not hammered.
    """
    from app.core.logsetup import redact

    meta = fetched.meta
    if meta.get("_circuit_open"):
        fetched.entities_failed[entity] = f"circuit open ({meta['_circuit_open']})"
        return None
    meta["_calls"] = meta.get("_calls", 0) + 1
    try:
        r = fn()
        fetched.entities_ok.append(entity)
        return r
    except Exception as e:  # noqa: BLE001
        fetched.entities_failed[entity] = redact(f"{type(e).__name__}: {e}")[:300]
        (log_ or log).warning("entity %s failed: %s", entity, fetched.entities_failed[entity])
        if meta["_calls"] <= 20:
            fc = meta.setdefault("_fail_classes", {})
            fc[type(e).__name__] = fc.get(type(e).__name__, 0) + 1
            if fc[type(e).__name__] > 10:
                meta["_circuit_open"] = type(e).__name__
                meta["message"] = f"circuit breaker opened after repeated {type(e).__name__}"
        return None


def sleep_for(seconds: float) -> float:
    """Trivial picklable target (tests of HardTimeoutRunner)."""
    time.sleep(seconds)
    return seconds
