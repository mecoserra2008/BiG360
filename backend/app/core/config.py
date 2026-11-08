"""Application settings.

Loads `<project_root>/.env` with case-insensitive key matching, so `Polygon_APIKEY`, `POLYGON_APIKEY` and
`polygon_apikey` all populate `Settings.polygon_apikey`. API keys are `SecretStr` and must never be logged;
use `Settings.keys_present()` to report availability.

Override the project root with env var BIG360_ROOT and the data directory with BIG360_DATA_DIR (used by tests).
Every setting, environment variable and config file is documented in docs/CONFIGURATION.md (keep it in sync).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _find_project_root() -> Path:
    env_root = os.environ.get("BIG360_ROOT")
    if env_root:
        return Path(env_root).resolve()
    # backend/app/core/config.py -> parents[3] == project root
    return Path(__file__).resolve().parents[3]


PROJECT_ROOT = _find_project_root()

#: env-var name (lower-case) of every secret, used by log redaction and keys_present()
SECRET_FIELDS: tuple[str, ...] = (
    "fred_apikey",
    "polygon_apikey",
    "edinet_apikey",
    "opendart_apikey",
    "companies_house_apikey",
    "bok_ecos_apikey",
    "openfigi_apikey",
    "krx_openapi_key",   # ingest-market: KRX Open API (optional)
    "banxico_token",     # ingest-market: Banxico SIE (optional)
    # macro-coverage (all optional: the macro jobs skip the source with a "missing key" status)
    "estat_appid",       # Japan e-Stat (CPI, labour, household survey)
    "kosis_apikey",      # Korea KOSIS
    "inegi_token",       # Mexico INEGI BIE (GDP, IGAE, unemployment, CPI)
    "bcch_user",         # Chile BCCh SieteRestWS user (e-mail) ...
    "bcch_pass",         # ... and password
    "eia_apikey",        # US EIA (energy prices / inventories)
    "comtrade_apikey",   # linkage: UN Comtrade keyed API (optional; keyless preview used otherwise)
    "finra_api_client_id",      # rates-w3: FINRA API (developer.finra.org) client credentials for TRACE aggregates ...
    "finra_api_client_secret",  # ... (optional; job credit_trace is skipped without them)
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- paths -----------------------------------------------------------------------------------
    project_root: Path = Field(default=PROJECT_ROOT, validation_alias=AliasChoices("BIG360_ROOT", "project_root"))
    data_dir_override: Path | None = Field(default=None, validation_alias=AliasChoices("BIG360_DATA_DIR"))

    # ---- API keys (names match .env exactly, matched case-insensitively) ---------------------------
    fred_apikey: SecretStr | None = None
    polygon_apikey: SecretStr | None = None
    edinet_apikey: SecretStr | None = None
    opendart_apikey: SecretStr | None = None
    companies_house_apikey: SecretStr | None = None
    bok_ecos_apikey: SecretStr | None = None
    openfigi_apikey: SecretStr | None = None
    krx_openapi_key: SecretStr | None = None   # optional (KR prices)
    banxico_token: SecretStr | None = None     # optional (MX macro)
    estat_appid: SecretStr | None = None       # optional (JP macro, e-Stat)
    kosis_apikey: SecretStr | None = None      # optional (KR macro, KOSIS)
    inegi_token: SecretStr | None = None       # optional (MX macro, INEGI)
    bcch_user: SecretStr | None = None         # optional (CL macro, BCCh SieteRestWS)
    bcch_pass: SecretStr | None = None         # optional (CL macro, BCCh SieteRestWS)
    eia_apikey: SecretStr | None = None        # optional (US energy, EIA)
    comtrade_apikey: SecretStr | None = None   # optional (linkage A2: UN Comtrade keyed API, comtradedeveloper.un.org)
    finra_api_client_id: SecretStr | None = None       # optional (rates-w3: FINRA API client credentials, TRACE stats)
    finra_api_client_secret: SecretStr | None = None
    sec_user_agent: str = "BiG Equity 360 admin@example.com"

    # ---- runtime -----------------------------------------------------------------------------------
    log_level: str = Field(default="INFO", validation_alias=AliasChoices("BIG360_LOG_LEVEL", "log_level"))
    api_host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("BIG360_API_HOST"))
    api_port: int = Field(default=8000, validation_alias=AliasChoices("BIG360_API_PORT"))
    #: CORS allow-list (O4). Comma list or JSON list. Default = the Vite dev server only; the built UI (app mode) and
    #: server-side .NET clients are same-origin / non-browser and need no CORS. Behind a gateway, list the gateway's
    #: public origin(s) here (never "*" together with credentials).
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"],
        validation_alias=AliasChoices("BIG360_CORS_ORIGINS"),
    )
    cors_origin_regex: str | None = Field(default=None, validation_alias=AliasChoices("BIG360_CORS_ORIGIN_REGEX"))
    cors_allow_credentials: bool = Field(default=False, validation_alias=AliasChoices("BIG360_CORS_ALLOW_CREDENTIALS"))
    #: reverse proxy / gateway (O4 Stage B): ASGI root_path when published under a path prefix (e.g. /big360), and
    #: whether uvicorn trusts X-Forwarded-For / -Proto from forwarded_allow_ips (comma list; the gateway's address)
    root_path: str = Field(default="", validation_alias=AliasChoices("BIG360_ROOT_PATH"))
    proxy_headers: bool = Field(default=False, validation_alias=AliasChoices("BIG360_PROXY_HEADERS"))
    forwarded_allow_ips: str = Field(default="127.0.0.1", validation_alias=AliasChoices("BIG360_FORWARDED_ALLOW_IPS"))

    # ---- observability (O4) --------------------------------------------------------------------------
    #: API JSON log file under data/logs ("" = no file; stderr only)
    api_log_file: str = Field(default="api.log", validation_alias=AliasChoices("BIG360_API_LOG_FILE"))
    #: one JSON access record per API request (logger app.api.access; health / ready / metrics at DEBUG)
    access_log: bool = Field(default=True, validation_alias=AliasChoices("BIG360_ACCESS_LOG"))
    #: JSON lines on stderr too (containers / log shippers); default human-readable text
    log_stderr_json: bool = Field(default=False, validation_alias=AliasChoices("BIG360_LOG_STDERR_JSON"))
    #: GET /metrics (Prometheus text format)
    metrics_enabled: bool = Field(default=True, validation_alias=AliasChoices("BIG360_METRICS"))
    #: GET /ready: require a live worker heartbeat (set false on an API-only node)
    ready_require_worker: bool = Field(default=True, validation_alias=AliasChoices("BIG360_READY_REQUIRE_WORKER"))
    ready_worker_max_age_s: float = Field(default=300.0, validation_alias=AliasChoices("BIG360_READY_WORKER_MAX_AGE_S"))

    # ---- authentication (O4; OFF by default - local use is unchanged) ------------------------------------
    #: none | oidc. oidc = every /api/ request needs an OIDC / JWT bearer token (Entra ID) - see app.api.auth
    auth_mode: str = Field(default="none", validation_alias=AliasChoices("BIG360_AUTH"))
    #: accepted `iss` values (comma list), e.g. https://login.microsoftonline.com/<tenant-id>/v2.0
    auth_issuer: Annotated[list[str], NoDecode] = Field(default_factory=list, validation_alias=AliasChoices("BIG360_AUTH_ISSUER"))
    #: accepted `aud` values (comma list), e.g. api://big360 or the app registration's client id
    auth_audience: Annotated[list[str], NoDecode] = Field(default_factory=list, validation_alias=AliasChoices("BIG360_AUTH_AUDIENCE"))
    #: JWKS URL; empty = discovered from <issuer>/.well-known/openid-configuration
    auth_jwks_url: str | None = Field(default=None, validation_alias=AliasChoices("BIG360_AUTH_JWKS_URL"))
    auth_algorithms: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["RS256"],
                                                            validation_alias=AliasChoices("BIG360_AUTH_ALGORITHMS"))
    auth_roles_claim: str = Field(default="roles", validation_alias=AliasChoices("BIG360_AUTH_ROLES_CLAIM"))
    #: token role value -> app role (viewer | analyst | admin); JSON object
    auth_role_map: dict[str, str] = Field(
        default_factory=lambda: {"BiG360.Viewer": "viewer", "BiG360.Analyst": "analyst", "BiG360.Admin": "admin"},
        validation_alias=AliasChoices("BIG360_AUTH_ROLE_MAP"))
    #: app role of a valid token that carries no mapped role (empty = 403)
    auth_default_role: str | None = Field(default=None, validation_alias=AliasChoices("BIG360_AUTH_DEFAULT_ROLE"))
    auth_leeway_s: int = Field(default=60, validation_alias=AliasChoices("BIG360_AUTH_LEEWAY_S"))
    auth_jwks_cache_s: int = Field(default=3600, validation_alias=AliasChoices("BIG360_AUTH_JWKS_CACHE_S"))
    #: only paths under these prefixes are protected (the static UI shell, /docs and /openapi.json stay public)
    auth_protected_prefixes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["/api/"], validation_alias=AliasChoices("BIG360_AUTH_PROTECTED_PREFIXES"))
    #: always public (probes and scrapers)
    auth_public_paths: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["/health", "/ready", "/metrics"], validation_alias=AliasChoices("BIG360_AUTH_PUBLIC_PATHS"))

    @field_validator("cors_origins", "auth_issuer", "auth_audience", "auth_algorithms", "auth_protected_prefixes",
                     "auth_public_paths", mode="before")
    @classmethod
    def _split_list(cls, v: Any) -> Any:
        """Accept a JSON list or a comma-separated string from the environment."""
        if isinstance(v, str):
            t = v.strip()
            if t.startswith("["):
                return json.loads(t)
            return [x.strip() for x in t.split(",") if x.strip()]
        return v

    @field_validator("auth_mode")
    @classmethod
    def _auth_mode(cls, v: str) -> str:
        m = (v or "none").strip().lower()
        m = {"off": "none", "": "none", "jwt": "oidc", "entra": "oidc"}.get(m, m)
        if m not in ("none", "oidc"):
            raise ValueError(f"BIG360_AUTH must be none or oidc, got {v!r}")
        return m
    duckdb_threads: int = Field(default=16, validation_alias=AliasChoices("BIG360_DUCKDB_THREADS"))
    #: DuckDB memory_limit of worker / job connections. DuckDB reads "GB" as 10^9 bytes: "12GB" = 11.18 GiB, the
    #: "11.1 GiB/11.1 GiB used" of the 2026-10-25/26 build_serving_db OOMs (the governor never sets this).
    duckdb_memory_limit: str = Field(default="12GB", validation_alias=AliasChoices("BIG360_DUCKDB_MEMORY"))
    #: refresh-pipeline fixes 2026-10-26: build_serving_db runs on its own, explicit DuckDB budget (memory_limit is
    #: DuckDB's buffer pool; the process peaks ~1-1.5 GB above it), thread count (fewer threads = smaller hash tables)
    #: and spill directory (local disk; the build spills a few GB at most).
    serving_duckdb_memory_limit: str = Field(default="7GB", validation_alias=AliasChoices("BIG360_SERVING_DUCKDB_MEMORY"))
    serving_duckdb_threads: int = Field(default=12, validation_alias=AliasChoices("BIG360_SERVING_DUCKDB_THREADS"))
    serving_temp_dir: str = Field(default="", validation_alias=AliasChoices("BIG360_SERVING_TEMP_DIR"))
    #: perf pass: DuckDB memory cap of the API's read-only serving connection (empty / 0 = DuckDB default, 80 % RAM)
    api_duckdb_memory_limit: str = Field(default="4GB", validation_alias=AliasChoices("BIG360_API_DUCKDB_MEMORY"))
    http_timeout_s: float = 30.0
    http_cache_default_ttl_s: int = 6 * 3600
    timezone: str = "America/New_York"
    #: timezone in which the worker interprets the dag crons of contracts/jobs.yaml (user's local time).
    #: Set BIG360_SCHEDULER_TZ=contract to use the jobs.yaml `timezone` instead.
    scheduler_timezone: str = Field(default="Europe/Lisbon", validation_alias=AliasChoices("BIG360_SCHEDULER_TZ", "scheduler_timezone"))
    #: max concurrent jobs in one DAG run (threads) and max concurrent CPU-heavy (process) jobs
    worker_max_jobs: int = Field(default=6, validation_alias=AliasChoices("BIG360_WORKER_MAX_JOBS", "worker_max_jobs"))
    #: ram_fix 2026-10-31: 2 -> 4. Medium and heavy jobs now run as processes; what may run side by side is decided
    #: by their RAM budgets (app.ops.ram_budget), this is only the upper bound
    worker_max_process_jobs: int = Field(default=4, validation_alias=AliasChoices("BIG360_WORKER_MAX_PROCESS_JOBS"))
    #: seconds between polls of the file job queue (data/state/queue)
    worker_poll_s: float = Field(default=5.0, validation_alias=AliasChoices("BIG360_WORKER_POLL_S"))
    #: JSON log lines (data/logs/*.log) — set BIG360_LOG_JSON=0 for plain text
    log_json: bool = Field(default=True, validation_alias=AliasChoices("BIG360_LOG_JSON", "log_json"))
    #: cross-process (file-backed) token buckets for hard-quota sources (see app.core.ratelimit.SHARED_SOURCES)
    shared_ratelimit: bool = Field(default=True, validation_alias=AliasChoices("BIG360_SHARED_RATELIMIT"))

    # ---- derived paths -----------------------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return Path(self.data_dir_override) if self.data_dir_override else self.project_root / "data"

    @property
    def contracts_dir(self) -> Path:
        return self.project_root / "contracts"

    @property
    def schemas_dir(self) -> Path:
        return self.contracts_dir / "schemas"

    @property
    def models_dir(self) -> Path:
        return self.project_root / "models_store"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def db_dir(self) -> Path:
        return self.data_dir / "db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / ".locks"

    @property
    def state_dir(self) -> Path:
        """Per-source cursors, job queue, worker heartbeat/lock, shared rate-limit state."""
        return self.data_dir / "state"

    @property
    def queue_dir(self) -> Path:
        """File-based job queue written by the API / CLI and polled by the worker (app.core.jobqueue)."""
        return self.state_dir / "queue"

    @property
    def job_logs_dir(self) -> Path:
        return self.logs_dir / "jobs"

    @property
    def warehouse_db_path(self) -> Path:
        """Worker-side DuckDB (views over Parquet, scratch). Never opened by the API."""
        return self.db_dir / "warehouse.duckdb"

    @property
    def app_state_db_path(self) -> Path:
        """Mutable user state (watchlists, alerts). Owned by the API process."""
        return self.db_dir / "app_state.duckdb"

    @property
    def serving_pointer_path(self) -> Path:
        """Text file containing the file name of the current serving snapshot (serving_<run_id>.duckdb)."""
        return self.db_dir / "CURRENT"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.raw_dir, self.cache_dir, self.db_dir, self.logs_dir, self.locks_dir, self.models_dir,
                  self.state_dir, self.queue_dir):
            p.mkdir(parents=True, exist_ok=True)

    # ---- secrets helpers -----------------------------------------------------------------------------
    def secret(self, name: str) -> str | None:
        """Return a secret's plain value (for building requests only — never log it)."""
        val = getattr(self, name.lower(), None)
        if isinstance(val, SecretStr):
            raw = val.get_secret_value().strip()
            return raw or None
        return None

    def keys_present(self) -> dict[str, bool]:
        return {name.removesuffix("_apikey"): self.secret(name) is not None for name in SECRET_FIELDS}

    def secret_values(self) -> list[str]:
        """All non-empty secret values (used by the log redaction filter)."""
        return [v for v in (self.secret(n) for n in SECRET_FIELDS) if v and len(v) >= 4]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """For tests: force re-read of environment / .env."""
    get_settings.cache_clear()
