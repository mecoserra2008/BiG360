# Configuration reference

This page lists every setting of BiG Equity 360: the environment variables, `.env`, the config files and the contract files. It also says where each one is read and what its default is. Keep it in sync with `backend/app/core/config.py`.

**Precedence, highest first:**
1. A process environment variable.
2. Windows Credential Manager. This applies to API keys only, and only when the supervisor or tray starts the processes.
3. `<project root>/.env`. Keys are matched case-insensitively, so `Polygon_APIKEY`, `POLYGON_APIKEY` and `polygon_apikey` are the same key.
4. The built-in default.

**When changes apply:**
- Settings are read once per process. Restart the affected child (API, worker or filler) after a change.
- The tray's "Quit" followed by a restart restarts all of them.
- The schedule profile (`PUT /api/v1/system/schedule`) is hot-reloaded by the worker and needs no restart.

**Secrets:**
- Secrets are pydantic `SecretStr` values.
- `GET /api/v1/system/status` reports only `keys_present` booleans.
- Every log handler redacts the key values and key-bearing query parameters.

---

## 1. Paths and runtime

| Variable | Default | Purpose |
|---|---|---|
| `BIG360_ROOT` | the folder that contains `backend/` | Project root: `contracts/`, `config/`, `models_store/`, `.env`. |
| `BIG360_DATA_DIR` | `<root>/data` | Data folder: lake, snapshots, state and logs. The installer writes it into `<install>\.env`. Tests point it at a temp folder. |
| `BIG360_CONFIG_DIR` | `<root>/config` | Location of `app.yaml`, `ops.yaml` and `schedule.yaml`. |
| `BIG360_MODELS_DIR` | `<data>/../models_store` | Trained-model folder. The installed app links `<root>/models_store` to it with a directory junction. |
| `BIG360_LOG_LEVEL` | `INFO` | Root log level. |
| `BIG360_LOG_JSON` | `1` | Write the file logs (`data/logs/*.log`) as JSON lines. `0` gives plain text. |
| `BIG360_LOG_STDERR_JSON` | `0` | Write JSON lines to stderr too. Use it for containers and log shippers. |
| `BIG360_DUCKDB_THREADS` | `16` | DuckDB threads per connection. |
| `BIG360_DUCKDB_MEMORY` | `12GB` | DuckDB `memory_limit` per connection (worker / jobs). Lower it on a shared server. DuckDB reads `GB` as 10^9 bytes: `12GB` = 11.18 GiB, the "11.1 GiB/11.1 GiB used" of the 2026-10-25/26 serving-build OOMs. |
| `BIG360_SERVING_DUCKDB_MEMORY` | `7GB` | `memory_limit` of the `build_serving_db` connection only (refresh-pipeline fixes 2026-10-26). The process peaks about 1.5 GB above it. |
| `BIG360_SERVING_DUCKDB_THREADS` | `12` | DuckDB threads of the serving build (fewer threads = smaller hash tables). |
| `BIG360_SERVING_TEMP_DIR` | `<data>/tmp/duckdb_serving` | Spill directory of the serving build (local disk; at most 60 GB). |
| `BIG360_HEAVY_SLOT` | `1` | Exclusive heavy slot (`app.ops.heavy_slot`): `build_serving_db` runs alone, heavy jobs wait, hourly news jobs are deferred, the gap filler yields. `0` disables it (tests). |
| `BIG360_HEAVY_SLOT_GB` / `BIG360_HEAVY_PEER_GB` | `7` / `3` | Coverage v3 A2: a job whose RAM budget (`mem_gb`) is at least `BIG360_HEAVY_SLOT_GB` takes the heavy slot exclusively, so no other job with a budget of `BIG360_HEAVY_PEER_GB` or more runs beside it in any runner. Bigs queue first in, first out, and the gap filler's heavy tasks wait. |
| `BIG360_RAM_FLOOR_GB` | `3` | Coverage v3 A2: admission floor. A job with a budget of 1 GB or more starts only when free RAM - reservations - its budget is at least this much. The ram_fix escapes ("alone", max wait) no longer bypass it. Below the floor the job waits, then is deferred (startup sync) or starts in spill mode: DuckDB is sized from what fits above the floor, Polars runs on the streaming engine and the Job Object cap is unchanged. `0` turns the floor off. |
| `BIG360_RAM_FLOOR_STRICT` | `0` | `1`: a job that cannot meet the floor is always deferred, never started in spill mode. |
| `BIG360_POLARS_STREAMING` | `1` | Coverage v3 A2: jobs with a budget of 7 GB or more (and jobs started in spill mode) run with `POLARS_ENGINE_AFFINITY=streaming` and `POLARS_TEMP_DIR=<data>/tmp/spill`. `0` keeps the in-memory engine. |
| `BIG360_SYNC_CAPS` | `1` | Coverage v3 A3: sync budget (`app.ops.sync.caps`). Each sync has a wall-clock budget: `max_wall_min` 120, or `startup_max_wall_min` 240 for the first sync after start. It also has `pass_max_jobs` (45) per pass. Work past the budget is skipped as `deferred` and carried to the next sync, first in line. Per-job timeouts come from run history: p95 x 1.5, clamped to [10, max(180, contract)] min. A hung sync holding the lock is stopped, then killed, by the supervisor. Keys are in `config/ops.yaml` `sync:`. `0` restores the previous behaviour; the no-overlap lock stays on. |
| `BIG360_SYNC_MAX_WALL_MIN` / `BIG360_SYNC_PASS_MAX_JOBS` | from `config/ops.yaml` | Override the sync budget and the per-pass job cap. |
| `BIG360_SYNC` | `1` | Startup sync (`app.ops.sync`, docs/STARTUP_SYNC_PLAN.md A): the supervisor runs it after the API is ready, every 30 min and on request (tray *Sync now*, `POST /api/v1/system/sync`). `0` turns it off (also `--no-sync`; settings in `config/ops.yaml` `sync`). |
| `BIG360_API_DUCKDB_MEMORY` | `4GB` | DuckDB `memory_limit` of the API's read-only serving connection. Empty or `0` means the DuckDB default (80 % of RAM). |
| `BIG360_STATUS_TTL_S` | `10` | Coverage v3 A7: `GET /system/status` is computed in a background thread (`app.api.status_cache`, single flight) and served from a cache; one refresh starts when the cached copy is older than this many seconds or was computed on another serving snapshot, while requests keep getting the cached copy (`as_of` / `cache` fields). `0` or below = compute on every request (tests). |
| `BIG360_STATUS_COLD_WAIT_S` | `10` | How long the first `/system/status` request of an API process waits for the first payload before it gets the light `cache.state: warming` payload. |
| `BIG360_STATUS_SECTION_MAX_REUSE_S` | `600` | A `/system/status` section that fails to refresh (freshness, DAG runs, counts, `ops_status` = scheduler / alerts, runtime) keeps its last good value for at most this many seconds (`cache.state: partial`, `cache.section_as_of`), then is served empty with its error. |
| `BIG360_SHARED_RATELIMIT` | `1` | File-backed token buckets shared across processes for hard-quota sources (Polygon, SEC, OECD). |
| `SEC_USER_AGENT` | `BiG Equity 360 admin@example.com` | User-Agent for SEC EDGAR. **Set a real contact e-mail.** SEC requires one, and in a bank deployment it should be a team mailbox. |

## 2. API server (FastAPI / uvicorn)

| Variable | Default | Purpose |
|---|---|---|
| `BIG360_API_HOST` | `127.0.0.1` | Bind address. Keep loopback unless a gateway on another host proxies to it. |
| `BIG360_API_PORT` | `8000` (CLI) | Port for `python -m app.cli serve api`. The supervisor, tray and app mode use `BIG360_SUPERVISOR_API_PORT` (default `8010`). |
| `BIG360_SUPERVISOR_API_PORT` | `8010` | API port that the supervisor starts. Also overrides `config/app.yaml` `api_port`. |
| `BIG360_SERVE_UI` | `0` (`1` in app mode) | Serve the built UI at `/` with SPA fallback. |
| `BIG360_UI_DIR` | first of `<root>/app_dist`, `<root>/frontend/dist_app` | Built UI folder. |
| `BIG360_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | **CORS allow-list.** Accepts a comma list or a JSON list. Only browser apps on another origin need an entry, such as the Vite dev server or a portal page on a different host. The built UI is same-origin, and .NET services are not browsers. Behind a gateway, list the gateway's public origin. Never use `*`. |
| `BIG360_CORS_ORIGIN_REGEX` | none | Optional origin regex, e.g. `https://.*\.bank\.internal`. |
| `BIG360_CORS_ALLOW_CREDENTIALS` | `0` | Allow cookies or `Authorization` headers on cross-origin browser calls. |
| `BIG360_ROOT_PATH` | empty | ASGI `root_path` for when the API is published under a path prefix (e.g. `/big360`) by IIS/ARR or YARP. Use it with a gateway that strips the prefix. |
| `BIG360_PROXY_HEADERS` | `0` | Trust `X-Forwarded-For` / `X-Forwarded-Proto` (uvicorn `proxy_headers`). |
| `BIG360_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Comma list of proxy addresses whose forwarded headers are trusted. |
| `BIG360_API_LOG_FILE` | `api.log` | API JSON log under `data/logs`. Empty means stderr only. |
| `BIG360_ACCESS_LOG` | `1` | One JSON access record per request. Fields: method, path, route, status, duration_ms, bytes, sse, client, principal and request_id. `/health`, `/ready` and `/metrics` are logged at DEBUG. With this on, uvicorn's own access log is off. |
| `BIG360_METRICS` | `1` | Enable `GET /metrics` (Prometheus text). `0` returns 404. |
| `BIG360_READY_REQUIRE_WORKER` | `1` | `GET /ready` requires a live worker heartbeat. Set `0` on an API-only node. |
| `BIG360_READY_WORKER_MAX_AGE_S` | `300` | Maximum age of the worker heartbeat for readiness. |

## 3. Authentication (off by default)

With `BIG360_AUTH=none` nothing is installed and local use is unchanged. Section 7 of [it/SECURITY.md](it/SECURITY.md) and [it/DEPLOYMENT_SERVER.md](it/DEPLOYMENT_SERVER.md) give the Entra ID set-up.

| Variable | Default | Purpose |
|---|---|---|
| `BIG360_AUTH` | `none` | `none` or `oidc`. `oidc` requires a valid bearer token on every path under the protected prefixes. Startup fails, closed, when issuer or audience is missing. |
| `BIG360_AUTH_ISSUER` | empty | Accepted `iss` values, as a comma list. Entra v2: `https://login.microsoftonline.com/<tenant-id>/v2.0`. Add `https://sts.windows.net/<tenant-id>/` if v1 tokens must be accepted. |
| `BIG360_AUTH_AUDIENCE` | empty | Accepted `aud` values: the API app registration's Application ID URI (`api://big360`) and/or its client id. |
| `BIG360_AUTH_JWKS_URL` | discovered | JWKS URL. When empty it is read from `<issuer>/.well-known/openid-configuration`. Entra: `https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys`. |
| `BIG360_AUTH_ALGORITHMS` | `RS256` | Allowed algorithms: RS256, RS384 and RS512. `none` and HMAC are always refused. |
| `BIG360_AUTH_ROLES_CLAIM` | `roles` | Claim that holds the roles. Entra app roles use `roles`; delegated scopes use `scp`. Dotted paths such as `realm_access.roles` also work. |
| `BIG360_AUTH_ROLE_MAP` | `{"BiG360.Viewer":"viewer","BiG360.Analyst":"analyst","BiG360.Admin":"admin"}` | JSON map from token role to app role (viewer < analyst < admin). Matching is case-insensitive. |
| `BIG360_AUTH_DEFAULT_ROLE` | empty | Role given to a valid token that has no mapped role. Empty means 403. |
| `BIG360_AUTH_LEEWAY_S` | `60` | Allowed clock skew for `exp` / `nbf` / `iat`. |
| `BIG360_AUTH_JWKS_CACHE_S` | `3600` | JWKS cache lifetime. An unknown `kid` forces a refresh, at most once a minute. |
| `BIG360_AUTH_PROTECTED_PREFIXES` | `/api/` | Paths that need a token. The UI shell, `/docs` and `/openapi.json` stay public. |
| `BIG360_AUTH_PUBLIC_PATHS` | `/health,/ready,/metrics` | Paths that are always public: probes and scrapers. |

**Role policy** (`app.api.auth.required_role`):

| Role | Allowed |
|---|---|
| viewer | Every GET, plus these read-only POSTs: screener query and export, backtest validate / compare / from-screen, studio preview / compare / leaderboard, supply-chain shock and portfolio exposure, macro scenario run. |
| analyst | All other writes: watchlists, alerts, theses, studies, backtests, scenarios. |
| admin | Job trigger, schedule change, model train and promote. |

**Crypto backend:**
- PyJWT[crypto] is used when it is installed.
- Otherwise a built-in RSA PKCS#1 v1.5 verifier is used. It is pure Python, does the public-key operation only and needs no extra dependency.
- For a server deployment install `PyJWT[crypto]>=2.8`.

## 4. Worker, scheduler and schedule profiles

| Variable | Default | Purpose |
|---|---|---|
| `BIG360_SCHEDULER_TZ` | `Europe/Lisbon` | Timezone of the jobs.yaml DAG crons for the worker. `contract` uses the jobs.yaml `timezone`. |
| `BIG360_SCHEDULE_PROFILE` | unset | Forces a profile (`weekly`, `daily` or `intraday`). It wins over the profile stored through the API (`data/state/schedule.json`) and over `default_profile`. |
| `BIG360_WORKER_MAX_JOBS` | `6` | Concurrent jobs in one DAG run (threads). |
| `BIG360_WORKER_MAX_PROCESS_JOBS` | `2` | Concurrent CPU-heavy process jobs. |
| `BIG360_WORKER_POLL_S` | `5` | Poll interval of the file job queue (`data/state/queue`). |
| `BIG360_REALTIME` | `0` | `1` makes the worker also run the realtime supervisor (R1 SSE bus). Same as `--realtime`. |
| `BIG360_BUS` | `sqlite` | Realtime bus backend. `redis` is a stub. |
| `BIG360_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis bus URL. Not used until the Redis bus is implemented. |

## 5. Desktop app, tray and supervisor

| Variable | Default | Purpose |
|---|---|---|
| `BIG360_APP` | `0` | Run the supervisor in app mode: it serves the UI. Set by the tray / `BiG360.exe`. |
| `BIG360_UPDATE_DIR` | `config/app.yaml` `update_dir` | Folder or share checked for newer builds. |
| `BIG360_CRED_PREFIX` | `BiG360/` | Windows Credential Manager target prefix for API keys. |
| `BIG360_CRED_BACKEND` | Windows: `wincred` | `keyring` uses the Python `keyring` package, which is the default on other OSes. |

## 6. QA / regression guard

| Variable | Default | Purpose |
|---|---|---|
| `BIG360_QA_API_URL` | probes `:<api_port>`, `:8010`, `:8000` | API that `python -m app.qa` and the `qa_regression` job test. |
| `BIG360_UI_URL` | API origin in app mode, else `http://localhost:5173` | UI that the smoke suite drives. |
| `BIG360_CHROME` | auto-detected | Path to `chrome.exe` / `msedge.exe` for the headless smoke suite. |

## 7. API keys (`.env` or Windows Credential Manager)

All keys are optional. A job whose key is missing reports "missing key" and skips that source.

| Key | Source |
|---|---|
| `FRED_APIKEY` | FRED / ALFRED (St. Louis Fed) |
| `POLYGON_APIKEY` | Massive (formerly Polygon.io) US prices, reference data and news |
| `EDINET_APIKEY` | EDINET (FSA Japan) |
| `OPENDART_APIKEY` | OpenDART (FSS Korea) |
| `COMPANIES_HOUSE_APIKEY` | UK Companies House |
| `BOK_ECOS_APIKEY` | Bank of Korea ECOS |
| `OPENFIGI_APIKEY` | OpenFIGI, for higher rate limits |
| `KRX_OPENAPI_KEY` | KRX Open API |
| `BANXICO_TOKEN` | Banxico SIE |
| `ESTAT_APPID` | Japan e-Stat |
| `KOSIS_APIKEY` | Korea KOSIS (declared, no fetcher yet) |
| `INEGI_TOKEN` | INEGI Mexico |
| `BCCH_USER` / `BCCH_PASS` | Banco Central de Chile SieteRestWS |
| `EIA_APIKEY` | US EIA (declared, no fetcher yet) |
| `COMTRADE_APIKEY` | UN Comtrade keyed API |

**Commands:**
- `python -m app.desktop.credentials migrate` moves the keys from `.env` into Credential Manager.
- `verify` checks them, and `status` lists them as booleans only.
- `.env` remains a development fallback.
- On a server, use Azure Key Vault or the Windows service account's Credential Manager ([it/SECURITY.md](it/SECURITY.md)).

**Vendor adapter.** `BIG360_PRICE_VENDOR` takes `none` (default), `file_drop`, `bloomberg`, `lseg`, `factset` or `internal`. It selects a licensed price vendor in `app.ingestion.vendors` (O4). It is not wired into the scheduled DAGs yet.

## 8. Frontend build variables (`frontend/.env*`, read by Vite at build time)

| Variable | Dev (`.env`) | App build (`.env.app`) | Purpose |
|---|---|---|---|
| `VITE_USE_MOCK` | `true` / `false` | `false` | Use the mock adapters instead of the API. |
| `VITE_API_URL` | `http://localhost:8000` (point it at `:8010` to use the supervisor's API) | `same-origin` | API base URL. `same-origin` means relative `/api/v1`. |
| `VITE_MOCK_FALLBACK` | `false` | `false` | Fall back to mocks on 501 or an unreachable API (development only). |

## 9. Config files

| File | Owner | Purpose | Applied |
|---|---|---|---|
| `.env` (root) | user / installer | API keys, `SEC_USER_AGENT`, `BIG360_DATA_DIR` and any `BIG360_*` above | Process start |
| `config/app.yaml` | desktop | Tray / supervisor: api_host, api_port (8010), serve_ui, filler, schedule, realtime, start_supervisor, status_poll_s, notifications, notify_severities, update_dir, update_check_h | Tray start |
| `config/ops.yaml` | ops | Gap-filler governor and housekeeping. Governor: ram_cap_gb 4, hard_factor 1.5, min_free_ram_gb 3, resume_free_ram_gb 3.5, max_concurrency 2, priority below_normal, pause_on_battery, pause_on_metered, min_disk_free_gb 15, yield_to_dags, tick_s 15. Filler: detect_every_h 24. Housekeeping: every_h 6, log_max_mb 50, log_keep 3, job_logs_max_age_days 45, snapshots_keep 2, disk_warn_gb 25, disk_min_gb 15. Sync (startup sync): enabled true, start_delay_s 90, interval_min 30, passes 2, stop_grace_s 120; yield_to_dags includes `sync` (the filler yields during the sync's pass 1). | Filler / supervisor restart |
| `config/schedule.yaml` | ops | Schedule profiles. weekly (default: Saturday 02:00 Lisbon, nightly → weekly → monthly-if-due, CFTC Friday 19:00 ET), daily, intraday placeholder. Also catch-up rules and misfire grace. | Hot reload |
| `data/state/schedule.json` | API | Profile chosen in the UI (`PUT /system/schedule`) | Hot reload |
| `contracts/api.yaml` | api | OpenAPI 3.1 contract (1.31.0), served as `/openapi.json`. Changes are additive only. | API start |
| `contracts/jobs.yaml` | platform | 159 jobs in 5 DAGs (nightly `30 1 * * 2-6`, weekly `0 6 * * 6`, monthly `0 8 1-7 * 0`, cftc_weekly `0 19 * * 5`, manual): dependencies, timeouts, retries, executor | Worker start |
| `contracts/coverage.yaml` | ops | Coverage registry: datasets, calendars, SLAs, fill actions and per-source rate budgets for the gap detector / filler | Filler / detector run |
| `contracts/schemas/*.yaml` | per owner | Table contracts: columns, types, PK, partitioning, point-in-time column. Enforced by `conform()` on every write. | Every write |
| `contracts/scoring.yaml`, `sector_kpis.yaml`, `sector_taxonomy.yaml`, `canonical_line_items*.yaml`, `macro_catalog.yaml` | analytics | Scoring weights, KPI registry, taxonomy, accounting concept chains, macro series catalogue | Job run |
| `packaging/requirements.lock.txt` | packaging | Locked Python dependency set for the portable build | Build |
| `VERSION` | packaging | App build version (1.0.0) | Build / tray |

**Change control.** Every change to `contracts/**` is logged in [CONTRACT_CHANGES.md](CONTRACT_CHANGES.md). For api.yaml, a minor bump means additive only. A major bump means breaking and needs orchestrator approval.
