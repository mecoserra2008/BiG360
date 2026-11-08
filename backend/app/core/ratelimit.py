"""Token-bucket rate limiting per source, usable from sync threads and asyncio tasks.

    limiter = get_limiter("sec_edgar")
    limiter.acquire()              # blocking (threads)
    await limiter.acquire_async()  # asyncio

Limits are per process by default. Sources with hard per-account / per-IP quotas (SHARED_SOURCES: SEC, Polygon/Massive,
OpenFIGI, GLEIF, Companies House, OECD, J-Quants ...) get a *cross-process* bucket whose state lives in
data/state/ratelimit/<source>.json behind a file lock, so the worker, the CLI and process-pool jobs share one budget
(research rule: "one rate limiter per host, global across threads and processes"). Disable with
BIG360_SHARED_RATELIMIT=0. Any file-system error falls back to the in-process bucket.

Daily request caps (OpenDART 20k/day, KRX 10k/day, ...) are tracked with get_daily_quota(source_id).

Defaults are conservative versions of each provider's published limits as reconciled with
docs/DATA_SOURCES_AMERICAS_EUROPE.md and docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md (the research docs win over
docs/ARCHITECTURE.md §9). Adapters may pass their own RateLimit; override at runtime with configure_limiter().
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RateLimit:
    rate_per_sec: float  # sustained refill rate
    burst: int = 1       # bucket capacity

    @classmethod
    def per_minute(cls, n: float, burst: int = 1) -> "RateLimit":
        return cls(rate_per_sec=n / 60.0, burst=burst)

    @classmethod
    def per_hour(cls, n: float, burst: int = 1) -> "RateLimit":
        return cls(rate_per_sec=n / 3600.0, burst=burst)


# source_id -> default limit.  Reconciled with the research docs (2026-10-10, platform agent); see the comment per line.
DEFAULT_LIMITS: dict[str, RateLimit] = {
    # ---------------- Americas / Europe (DATA_SOURCES_AMERICAS_EUROPE.md)
    "sec_edgar": RateLimit(8.0, 8),                 # SEC fair access <= 10 req/s per user across machines; use 8/s
    "polygon": RateLimit.per_minute(4, burst=1),    # free tier 5/min; research: api.massive.com + api.polygon.io share
                                                    # one account quota, budget 4/min (was 5/min)
    "yfinance": RateLimit(0.5, 2),                  # unofficial; price batches of 200-500 tickers per call
    "yfinance_fundamentals": RateLimit(1.0, 1),     # research: per-ticker .info/statements <= ~1 ticker/s + pauses
    "fred": RateLimit(1.5, 3),                      # documented 120 req/min; research budget <= 2 req/s
    "alfred": RateLimit(1.5, 3),
    "esef_xbrl_org": RateLimit(1.0, 1),             # filings.xbrl.org: no documented limit, research says <= 1 req/s (was 2/s)
    "cvm": RateLimit(2.0, 2),                       # static zip/csv downloads (dados.cvm.gov.br)
    "b3": RateLimit(1.0, 1),                        # B3 COTAHIST zips
    "companies_house": RateLimit(2.0, 5),           # 600 req / 5 min
    "fca_nsm": RateLimit(1.0, 1),                   # undocumented API, research: <= 1 req/s
    "bafin": RateLimit(1.0, 1),                     # displaytag CSV export, be polite
    "amf": RateLimit(1.0, 1),
    "afm": RateLimit(1.0, 1),
    "esma": RateLimit(1.0, 1),                      # FIRDS / registers
    # coverage-B national OAMs / registries (COMPANY_COVERAGE_OPTIONS §3/§5; undocumented limits -> <= 1 req/s)
    "dila_fr": RateLimit(1.0, 1),                   # info-financiere.gouv.fr OpenDataSoft API + opendatasoft FTP mirror
    "afm_nl": RateLimit(1.0, 1),                    # AFM financial-reporting register export + detail pages
    "fsma_be": RateLimit(1.0, 1),                   # FSMA STORI web API
    "emarket_it": RateLimit(1.0, 1),                # eMarket Storage (Spafid) issuer pages / 1Info
    "nasdaq_nordic": RateLimit(1.0, 1),             # api.news.eu.nasdaq.com (undocumented)
    "oslo_newsweb": RateLimit(1.0, 1),              # api3.oslo.oslobors.no newsreader (undocumented)
    "cnmv_es": RateLimit(0.5, 1),                   # CNMV portal: WAF (403 CVFE) on bursts/malformed requests
    "biva_mx": RateLimit(1.0, 1),                   # biva.mx banco-informacion JSON + storage
    "cmf_cl": RateLimit(0.5, 1),                    # cmfchile.cl ASP pages (302-empty quirk; retry slowly)
    "smv_pe": RateLimit(1.0, 1),                    # SMV SIMV ASP.NET postbacks (session-bound detail pages)
    "openfigi": RateLimit.per_minute(20, burst=1),  # /v3/mapping 25/min without key (keep margin)
    "openfigi_keyed": RateLimit(4.0, 4),            # with OPENFIGI_APIKEY: 25 req / 6 s (~4.2/s), 100 jobs/request
    "gleif": RateLimit.per_minute(50, burst=1),     # 60 req/min documented, research: throttle at 50/min (was 60/min)
    "finra": RateLimit(2.0, 2),
    # ---------------- Asia (DATA_SOURCES_ASIA_MACRO_EVENTS.md)
    "edinet": RateLimit(1.0, 1),                    # no published limit; research: sequential ~1 req/s (burst 2 -> 1)
    "tdnet": RateLimit(1.0, 1),
    "jpx": RateLimit(1.0, 1),
    "jquants": RateLimit.per_minute(5, burst=1),    # free plan 5 req/min (429 + 5-min block on abuse)
    "opendart": RateLimit(3.0, 3),                  # research budget 3 req/s (bursts <= 5-10 trigger IP blocks); 20k/day cap
    "krx": RateLimit(1.0, 2),                       # KRX Open API, 10k calls/day cap
    "naver": RateLimit(1.0, 1),
    "akshare": RateLimit(1.0, 1),                   # scrapes Eastmoney/Sina; be gentle (Sina ~4 s/stock)
    "baostock": RateLimit(2.0, 2),                  # sequential session API
    "twse": RateLimit(0.3, 1),                      # www.twse.com.tw rwd archives: IP block after ~3 req / 5 s
    "tpex": RateLimit(0.3, 1),                      # www.tpex.org.tw daily quotes archive: same policy as TWSE
    "tushare": RateLimit.per_minute(50, burst=1),   # free 120-pt tier: 50 calls/min, 8k/day
    "cninfo": RateLimit(1.0, 1),
    "sse": RateLimit(1.0, 1),
    "szse": RateLimit(1.0, 1),
    "hkex": RateLimit(1.0, 1),
    # ---------------- macro / events
    "ecb": RateLimit(2.0, 2),
    "eurostat": RateLimit(1.0, 2),
    "boe": RateLimit(1.0, 1),
    "boj": RateLimit(1.0, 1),
    "bcb": RateLimit(1.0, 2),
    "banxico": RateLimit(0.6, 2),                   # 200 queries / 5 min (historical endpoint)
    "bis": RateLimit(1.0, 1),
    "dbnomics": RateLimit(2.0, 2),
    "worldbank": RateLimit(2.0, 2),
    "oecd": RateLimit.per_minute(0.9, burst=3),     # research: 60 data downloads / HOUR (was 0.5/s = 1800/h!)
    "imf": RateLimit(1.0, 1),
    "bok_ecos": RateLimit(2.0, 2),
    # macro-coverage (contracts/macro_catalog.yaml sources; all public statistical APIs, polite budgets)
    "bundesbank": RateLimit(2.0, 2),
    "snb": RateLimit(1.0, 2),
    "insee": RateLimit(1.0, 1),                     # BDM SDMX: documented 30 req/min
    "bpstat": RateLimit(1.0, 1),
    "ine_es": RateLimit(1.0, 1),
    "ust": RateLimit(1.0, 2),                       # home.treasury.gov yearly CSV files
    "statcan": RateLimit(1.0, 1),                   # WDS: 50 req/s documented, but resets bursts
    "boc": RateLimit(2.0, 2),
    "ibge": RateLimit(1.0, 1),
    "bcrp": RateLimit(1.0, 1),
    "bcra": RateLimit(1.0, 1),
    "datos_ar": RateLimit(1.0, 1),
    "banrep": RateLimit(0.5, 1),                    # ShieldSquare bot wall in front of the SDMX endpoint
    "ine_cl": RateLimit(1.0, 1),
    "bcch": RateLimit(1.0, 1),
    "inegi": RateLimit(1.0, 1),
    "ons": RateLimit(1.0, 2),                       # www.ons.gov.uk rate-limits bursts (120 req / 10 s)
    "mof_jp": RateLimit(1.0, 1),
    "estat_jp": RateLimit(1.0, 1),
    "hkma": RateLimit.per_minute(6, burst=1),       # Aliyun WAF blocks bursts with HTTP 405 for 20+ min
    "censtatd": RateLimit(1.0, 1),
    "rba": RateLimit(1.0, 1),
    "abs": RateLimit(1.0, 1),
    "cbc_tw": RateLimit(1.0, 1),
    "nyfed": RateLimit(0.5, 1),                     # newyorkfed.org ACM xls (one ~10 MB file per run; macro-w1)
    "gdelt_doc": RateLimit(0.2, 1),                 # DOC 2.0 API: server enforces "one every 5 seconds"
    "gdelt_files": RateLimit(4.0, 4),               # static 15-min export/GKG files on data.gdeltproject.org
    "huggingface": RateLimit(2.0, 2),
    # linkage (A1-A5): Comtrade keyless preview answers 429 above ~1 call / 3-4 s
    "comtrade": RateLimit(0.25, 1),
    "natural_earth": RateLimit(1.0, 1),
    "default": RateLimit(1.0, 1),
}

#: source ids that are the same quota (same host / same account) -> canonical bucket
ALIASES: dict[str, str] = {
    "massive": "polygon",
    "polygon_io": "polygon",
    "sec": "sec_edgar",
    "edgar": "sec_edgar",
    "filings_xbrl_org": "esef_xbrl_org",
    "dart": "opendart",
}

#: sources whose quota is enforced per account / IP: one bucket shared by all processes on this machine
SHARED_SOURCES: frozenset[str] = frozenset(
    {"sec_edgar", "polygon", "openfigi", "openfigi_keyed", "gleif", "companies_house", "oecd", "jquants", "gdelt_doc",
     "opendart", "tushare", "yfinance", "yfinance_fundamentals", "twse", "tpex"}
)

#: hard daily request caps (calls per day) enforced via get_daily_quota(); (cap, timezone of the reset)
DAILY_CAPS: dict[str, tuple[int, str]] = {
    "opendart": (19_500, "Asia/Seoul"),   # 20,000/day per key (margin 500)
    "krx": (9_800, "Asia/Seoul"),         # 10,000/day
    "tushare": (7_800, "Asia/Shanghai"),  # 8,000/day (free tier)
    "banxico": (9_800, "America/Mexico_City"),
}


def canonical_source(source_id: str) -> str:
    return ALIASES.get(source_id, source_id)


class TokenBucket:
    """Thread-safe token bucket with blocking sync and async acquire."""

    def __init__(self, limit: RateLimit, name: str = ""):
        if limit.rate_per_sec <= 0 or limit.burst < 1:
            raise ValueError("rate_per_sec must be > 0 and burst >= 1")
        self.name = name
        self.limit = limit
        self._tokens = float(limit.burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.total_acquired = 0
        self.total_wait_s = 0.0

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(float(self.limit.burst), self._tokens + elapsed * self.limit.rate_per_sec)
            self._last = now

    def _try_take(self, tokens: float) -> float:
        """Take tokens if available and return 0, else return seconds to wait (nothing taken)."""
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self._tokens >= tokens:
                self._tokens -= tokens
                self.total_acquired += 1
                return 0.0
            return (tokens - self._tokens) / self.limit.rate_per_sec

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        if tokens > self.limit.burst:
            raise ValueError("tokens > burst capacity")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait = self._try_take(tokens)
            if wait == 0.0:
                return True
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            self.total_wait_s += wait
            time.sleep(wait)

    async def acquire_async(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        if tokens > self.limit.burst:
            raise ValueError("tokens > burst capacity")
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            wait = self._try_take(tokens)
            if wait == 0.0:
                return True
            if deadline is not None and loop.time() + wait > deadline:
                return False
            self.total_wait_s += wait
            await asyncio.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """Drain the bucket and push the next token `seconds` into the future (used on HTTP 429 / Retry-After)."""
        with self._lock:
            self._tokens = -seconds * self.limit.rate_per_sec
            self._last = time.monotonic()


class SharedTokenBucket(TokenBucket):
    """Token bucket whose state is shared by all processes via a JSON file guarded by a file lock.

    Wall-clock time (time.time) is used for refill so processes agree. On any I/O problem the bucket degrades to the
    in-process behaviour of TokenBucket (never blocks forever on a broken state file).
    """

    def __init__(self, limit: RateLimit, name: str, state_dir: Path):
        super().__init__(limit, name)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_dir / f"{name}.json"
        self.lock_path = state_dir / f"{name}.lock"
        from filelock import FileLock  # local import keeps module import cheap

        self._flock = FileLock(str(self.lock_path), timeout=10)
        self.degraded = False

    def _read_state(self, now: float) -> tuple[float, float]:
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
            return float(st["tokens"]), float(st["last"])
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return float(self.limit.burst), now

    def _write_state(self, tokens: float, last: float) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"tokens": tokens, "last": last}), encoding="utf-8")
        tmp.replace(self.state_path)

    def _try_take(self, tokens: float) -> float:
        if self.degraded:
            return super()._try_take(tokens)
        try:
            with self._lock, self._flock:
                now = time.time()
                cur, last = self._read_state(now)
                if now > last:
                    cur = min(float(self.limit.burst), cur + (now - last) * self.limit.rate_per_sec)
                    last = now
                if cur >= tokens:
                    self._write_state(cur - tokens, last)
                    self.total_acquired += 1
                    return 0.0
                self._write_state(cur, last)
                return (tokens - cur) / self.limit.rate_per_sec
        except Exception:  # Timeout / OSError / PermissionError -> fall back to process-local limiting
            self.degraded = True
            return super()._try_take(tokens)

    def penalize(self, seconds: float) -> None:
        super().penalize(seconds)
        if self.degraded:
            return
        try:
            with self._flock:
                self._write_state(-seconds * self.limit.rate_per_sec, time.time())
        except Exception:
            self.degraded = True


_registry: dict[str, TokenBucket] = {}
_registry_lock = threading.Lock()


def _make_bucket(source_id: str, limit: RateLimit) -> TokenBucket:
    if source_id in SHARED_SOURCES:
        try:
            from .config import get_settings

            s = get_settings()
            if s.shared_ratelimit:
                return SharedTokenBucket(limit, source_id, s.state_dir / "ratelimit")
        except Exception:
            pass
    return TokenBucket(limit, source_id)


def get_limiter(source_id: str, limit: RateLimit | None = None) -> TokenBucket:
    """Process-wide limiter for a source (aliases share one bucket). First caller's `limit` (or the default) wins."""
    key = canonical_source(source_id)
    with _registry_lock:
        b = _registry.get(key)
        if b is None:
            b = _make_bucket(key, limit or DEFAULT_LIMITS.get(key, DEFAULT_LIMITS["default"]))
            _registry[key] = b
        return b


def configure_limiter(source_id: str, limit: RateLimit) -> TokenBucket:
    """Replace a source's limiter (e.g. when an API key unlocks a higher tier)."""
    key = canonical_source(source_id)
    with _registry_lock:
        b = _make_bucket(key, limit)
        _registry[key] = b
        return b


def reset_limiters() -> None:
    """For tests: forget all process-wide buckets."""
    with _registry_lock:
        _registry.clear()


# ------------------------------------------------------------------------------------------------ daily quotas


class QuotaExceeded(RuntimeError):
    pass


class DailyQuota:
    """File-backed per-day request counter shared by all processes (data/state/ratelimit/<source>.quota.json)."""

    def __init__(self, source_id: str, cap: int, tz: str = "UTC", state_dir: Path | None = None):
        from filelock import FileLock

        from .config import get_settings

        self.source_id, self.cap, self.tz = source_id, int(cap), ZoneInfo(tz)
        d = state_dir or get_settings().state_dir / "ratelimit"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{source_id}.quota.json"
        self._flock = FileLock(str(d / f"{source_id}.quota.lock"), timeout=10)

    def _today(self) -> str:
        return datetime.now(UTC).astimezone(self.tz).date().isoformat()

    def _read(self) -> dict:
        try:
            st = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            st = {}
        if st.get("day") != self._today():
            st = {"day": self._today(), "used": 0}
        return st

    def used(self) -> int:
        with self._flock:
            return int(self._read()["used"])

    def remaining(self) -> int:
        return max(0, self.cap - self.used())

    def try_consume(self, n: int = 1) -> bool:
        """Count n calls if they fit in today's cap. Returns False (nothing counted) when the cap would be exceeded."""
        with self._flock:
            st = self._read()
            if st["used"] + n > self.cap:
                return False
            st["used"] += n
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(st), encoding="utf-8")
            tmp.replace(self.path)
            return True

    def consume(self, n: int = 1) -> None:
        if not self.try_consume(n):
            raise QuotaExceeded(f"{self.source_id}: daily cap {self.cap} reached")


def get_daily_quota(source_id: str) -> DailyQuota | None:
    """DailyQuota for sources listed in DAILY_CAPS, else None."""
    key = canonical_source(source_id)
    spec = DAILY_CAPS.get(key)
    return DailyQuota(key, spec[0], spec[1]) if spec else None
