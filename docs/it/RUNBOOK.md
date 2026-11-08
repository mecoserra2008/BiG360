# Runbook

**Covers:** day-to-day operation of BiG Equity 360 on a workstation (Stage A) or a server (Stage B).

**Conventions:**
- Commands run in PowerShell from `<root>\backend`, where `<root>` is the project or install folder.
- `$py` is the Python of the installation:
  - `..\.venv\Scripts\python.exe` in the dev tree;
  - `..\python\python.exe` in the portable / installed build.
- **Data folder:** `BIG360_DATA_DIR`, by default `<root>\data`.

---

## 1. Install

| Target | Steps |
|---|---|
| **Workstation, installer** | 1. Run `BiG360Setup-<version>.exe` (per user, no admin rights). 2. Choose the existing data folder; it is not copied. 3. Optional tasks: start at login, and copy the API keys from `.env` into Credential Manager. |
| **Workstation, portable** | 1. Unzip `BiG360-<version>-portable.zip`. 2. Set `BIG360_DATA_DIR=<data folder>` in `<install>\.env`. 3. Start `BiG360.exe`. |
| **Server (Stage B)** | 1. Copy the portable folder to `D:\BiG360\app` and create `D:\BiG360\data` (or restore a data backup). 2. Install the WinSW service ([DEPLOYMENT_SERVER.md §1.1](DEPLOYMENT_SERVER.md#11-windows-service-with-winsw)). 3. Store the keys in the service account's Credential Manager (`runas /user:BANK\svc-big360$ ... credentials set <key>`) or Key Vault. 4. Configure the gateway. |
| **Build a release** | `.\packaging\build.ps1 -Zip [-Installer] [-PublishTo \\share\big360\updates]`. The installer needs Inno Setup 6. `npm run build:app` (frontend) is part of the build. |

**First-run checks:**

```powershell
& $py -m app.ops.supervisor --status          # children running?
curl http://127.0.0.1:8010/health             # {"status":"ok", ...}
curl -i http://127.0.0.1:8010/ready           # 200 when the snapshot + worker heartbeat are OK, else 503 + checks
& $py -m app.desktop.credentials status       # which keys are stored (booleans only)
& $py -m app.cli doctor                       # source reachability / keys
```

## 2. Start and stop

| Action | Workstation (tray) | Supervisor (any) | Server (service) |
|---|---|---|---|
| Start | `BiG360.exe` | `.\scripts\run_supervisor.ps1 [-App]`, or `& $py -m app.ops.supervisor --app` | `sc start BiG360` |
| Stop (graceful) | Tray › Quit | `.\scripts\run_supervisor.ps1 -Stop`, which creates `data\state\ops\supervisor.STOP` | `sc stop BiG360` |
| Status | Tray › Status | `& $py -m app.ops.supervisor --status`, or `data\logs\app\supervisor.json` | Same |
| Restart **only the API** (e.g. after a contract or code change) | | `Stop-Process -Id (Get-Content ..\data\logs\app\api.pid)`. The supervisor restarts it after 5 s. Check that `/openapi.json` shows the new `info.version`. | Same |
| Pause background filling | Tray › Pause | `python -m app.ops.pause set --reason REASON --hours 24` (`clear` resumes, `status` shows the state). The flag expires at its `until=`, 24 h by default. A hand-made file without `until=` expires 7 days after it was written | Same |
| Run a refresh now | Tray › Run refresh now | `& $py -m app.cli jobs run-dag nightly --resume`, or `POST /api/v1/system/jobs/{job}/trigger` | Same |
| Change the schedule profile | Tray › Schedule | `PUT /api/v1/system/schedule {"profile":"daily"}` (hot reload) | Same |

**What graceful stop does:**
1. Children get Ctrl+Break.
2. The worker finishes or cancels its running jobs; the grace period is 300 s.
3. Any process tree still left is killed.

A stale `*.STOP` file left by a previous shutdown is removed on the next start.

## 3. Upgrade

1. **Read the release notes and `docs/CONTRACT_CHANGES.md`:**
   - API minor bumps are additive. .NET clients keep working (INTEGRATION_DOTNET.md §7).
   - Table-contract changes are applied by the jobs themselves. Nullable columns are added on read, so no migration step is needed.
2. **Back up `app_state`** (§5.1).
3. **Run the new installer.** It stops BiG360 gracefully, replaces the program files and keeps the data. On a server: stop the service, replace `D:\BiG360\app`, then start the service.
4. **Verify:**
   - `GET /openapi.json` shows the new `info.version`.
   - `GET /ready` returns 200.
   - Run `& $py -m app.qa` (regression guard: API contract sweep, golden data checks, UI smoke). Its result also shows on Data Health.
5. **Rollback:** reinstall the previous version. The data is forward-compatible within a major contract version: new columns are nullable and older code ignores them.

The update check is configured in `config\app.yaml` (`update_dir`). It looks for `latest.json` or `BiG360Setup-<ver>.exe` on a share. The tray shows "Update available".

## 4. Health checks and routine monitoring

| Check | Healthy |
|---|---|
| `GET /ready` | 200; `serving_snapshot` ok; `worker_heartbeat` ok |
| `GET /api/v1/system/status` | `data_as_of_overall` within the SLA (weekly profile: up to 9 days); `scheduler.enabled = true`; no `error` ops alerts |
| Data Health page (`/system/data-health`) | No red datasets; gap queue draining; campaigns progressing |
| `GET /metrics` | `big360_worker_up 1`, `big360_filler_up 1`, `big360_serving_snapshot_age_seconds` below the SLA |
| Disk | More than 25 GB free on the data volume. Housekeeping warns below 25 GB and the filler pauses below 15 GB. |

## 5. Backup and restore

### 5.1 app_state: user data that cannot be rebuilt

`data\db\app_state.duckdb` holds watchlists, alerts, theses and journal entries, studies, backtest definitions, scenarios and model promotions. The API holds it open with a lock, so stop the API to copy it:

```powershell
$d = "..\data\db"; $ts = Get-Date -Format yyyyMMdd_HHmm
Stop-Process -Id (Get-Content ..\data\logs\app\api.pid)          # the supervisor restarts it after 5 s - copy quickly,
Start-Sleep 1                                                    # or stop the whole supervisor / service for a clean window
Copy-Item "$d\app_state.duckdb" "\\backup\big360\app_state_$ts.duckdb"
if (Test-Path "$d\app_state.duckdb.wal") { Copy-Item "$d\app_state.duckdb.wal" "\\backup\big360\app_state_$ts.duckdb.wal" }
```

For a scheduled backup, use a service-stop window: stop the service, copy the files, start the service. After the SQL Server move (DEPLOYMENT_SERVER.md §6), this becomes a standard SQL backup.

**Restore:**
1. Stop BiG360.
2. Copy the backup over `app_state.duckdb`, removing any newer `.wal` file.
3. Start BiG360.
4. Check the Watchlists and Theses pages.

### 5.2 The data lake

| Folder | Back up? | Restore |
|---|---|---|
| `normalized\`, `reference\` (about 8 GB) | **Yes**, daily incremental. Parquet partitions are immutable files. | Copy back, then run `& $py -m app.cli jobs run-dag nightly --resume` to rebuild everything derived. |
| `raw\` (most of the 122 GB) | Yes, weekly. It is the landing zone and lets normalisation be re-run without re-downloading. | Copy back. Without it, the sources would be re-downloaded by the gap filler, which takes days for GDELT. |
| `features\`, `scores\`, `ml\`, `modeling\`, `db\serving_*` | Optional | Rebuilt by a full refresh in about 3–6 h |
| `state\` (cursors, schedule choice, campaign progress) | Yes (small) | Copy back. Without it, sources re-scan from their earliest watermark. |
| `models_store\` | Yes, weekly | Copy back, or retrain (weekly DAG) |
| `logs\` | Per log policy | n/a |

**Restore the serving snapshot only** (for example after a bad publish): `data\db\` keeps the previous snapshot. Write its file name into `data\db\CURRENT`. The API switches within about 1 s; no restart is needed.

## 6. Troubleshooting

These failure modes have occurred on this installation, with their symptoms and fixes.

| Symptom | Cause (seen before) | Fix |
|---|---|---|
| **Out of memory.** `MemoryError` in a job log; a job run marked *Orphaned (process died)*; the API child exits with code 1 and the supervisor restarts it (seen 2026-10-20 22:02 UTC during a heavy UI sweep, API RSS about 6.7 GB). | Several heavy processes at once: a serving build plus the GDELT E6 backfill plus test or agent processes exhausted the commit charge (2026-10-19 03:31). `lazy_prices` MemoryError. Weekly `sec_13f` upsert risk. | (1) Let the filler governor pause work: `config\ops.yaml` `ram_cap_gb` 4, `min_free_ram_gb` 3, `yield_to_dags`. (2) Do not run manual heavy jobs during the Saturday refresh. (3) Cap DuckDB: `BIG360_DUCKDB_MEMORY` (jobs) and `BIG360_API_DUCKDB_MEMORY` (API, default 4 GB). (4) Re-run the failed job: `& $py -m app.cli jobs run <job_id>`, or let the next refresh resume it (`run-dag ... --resume`). |
| **GDELT day partial or missing**, e.g. 2025-05-13 with 512 of 576 files; events map gaps; Data Health shows *partial* | Network or DNS loss while the laptop slept; occasional GDELT 404s or outages for single 15-minute files | The gap detector classifies these as `partial` and the filler re-runs them (`gdelt_redo_partial`). Manually: `& $py -m app.events_v2.backfill --label redo --start 2025-05-13 --end 2025-05-13 --redo`. Files that GDELT never published are marked *unfillable*; the detector stops retrying them. |
| **Job timed out** ("timed out after 60.00 min") for `prices_global_yf`, `sec_text`, `yf_fundamentals`, `gdelt_stream` or `earnings_dates_fetch` | Slow sources, rate limits, or a hung thread | Since O1, thread jobs are cancelled cooperatively, then killed and retried, and the heavy ones run as processes. If a job still times out, raise its `timeout_min` in `contracts/jobs.yaml` (a logged contract change) or check the source's status. |
| **No refresh happened** / data 3+ days old after a weekend | The PC slept at the scheduled time (this caused a 3-day loss), or the worker was started with `--no-schedule` (Scheduler off) | The worker keeps the PC awake during runs, arms a wake timer and catches up at start or within 15 min. Check `/api/v1/system/status` → `scheduler` (`enabled`, `next_runs`, last completed run per DAG). Start the supervisor without `--no-schedule`. Allow wake timers in the Windows power plan (*Allow wake timers: Enable*). |
| **Header shows "Data stale" / an ops alert says the refresh is overdue** | A refresh DAG failed partway; `serving_sections` marks the stale part | Data Health › Recent failures → open the job log (`data\logs\jobs\<job>\<run>.log`). Fix the cause, then run `& $py -m app.cli jobs run-dag nightly --resume`. Prices are published even when an analytics job fails. |
| **Regression guard failed** (ops alert `qa_regression`) | A contract sweep, golden check or UI smoke failure after a refresh, or **the API was not restarted after a contract change**, so the served version differs from `contracts/api.yaml` | `GET /api/v1/system/qa?detail=true` lists the failing items. If the issue is "served vs contract version", restart the API child (§2). |
| **Filler crashed every tick** with `OSError [Errno 22]` on `data\state\ops\tasks\gap_prices.CN_*.json` (2026-10-20) | Task id contained `*` | Fixed: task ids are made Windows-safe (`app.ops.tasks.safe_name`). If it recurs, check `data\logs\filler.log`. |
| **"api already running outside the supervisor"** / UI not served at `/` | The port was already in use, e.g. a dev API on :8010 | The supervisor marks the child `external` and re-checks every minute. Stop the other process, or use another port (`--api-port`). |
| **A source reports "missing key"** | No key in Credential Manager or `.env` | `& $py -m app.desktop.credentials set <key>`. Keys are optional; jobs skip the source. |
| **SEC returns 403** | Missing or placeholder `SEC_USER_AGENT` | Set `SEC_USER_AGENT="BiG Equity 360 <team mailbox>"` in `.env` or the service environment. |
| **Polygon / Massive 429** | Free tier allows 5 calls per minute per account | The shared token bucket (`BIG360_SHARED_RATELIMIT=1`) serialises calls across processes. Do not run a backfill and the nightly job in parallel. |
| **`/ready` returns 503 with `worker_heartbeat` FAIL** | The worker is stopped or hung | `supervisor --status`. The supervisor restarts a crashed worker. A hung worker (heartbeat older than 5 min while its process is alive) needs a stop and start. On an API-only node, set `BIG360_READY_REQUIRE_WORKER=0`. |
| **`/health` slow (several seconds)** | The API thread pool is saturated by heavy requests; the health check computes the DQ summary | Usually transient. The supervisor restarts the API after 3 failed health checks (10 s timeout each). If it persists, look at `big360_http_request_duration_seconds` by route in `/metrics` for the slow endpoint. |
| **Live strip shows "Not live"** | The realtime layer is off (default) | Start the worker with `--realtime` (`config\app.yaml` `realtime: true`) if the live feeds are wanted |
| **Corporate network: SSL errors on every source** | TLS inspection root CA not trusted by Python | Set `SSL_CERT_FILE` to a PEM bundle with the bank roots (DEPLOYMENT_SERVER.md §3) and `HTTPS_PROXY` |

## 7. Useful commands

```powershell
& $py -m app.cli jobs list                         # jobs, last status
& $py -m app.cli jobs run <job_id> [--force]       # run one job now (in this process)
& $py -m app.cli jobs enqueue <job_id>             # queue it for the worker (what the API does)
& $py -m app.cli jobs run-dag nightly --resume          # catch-up / resume a failed refresh
& $py -m app.cli db build-serving                  # rebuild and publish the serving snapshot
& $py -m app.ops.gaps                              # run the gap detector now
& $py -m app.qa                                    # regression guard (API sweep, golden checks, UI smoke)
& $py -m app.ops.export --out D:\exports --format parquet     # snapshot export for SQL Server / BI
& $py ..\scripts\make_sbom.py                      # regenerate docs\it\sbom.json
```

## 8. Log locations

All under `data\logs\`. File logs are JSON lines, one object per line, with keys `ts`, `level`, `logger`, `msg`, `service`, `request_id`, `job_id`, `run_id`, `exc`. Secrets are redacted.

| File | Content | Rotation |
|---|---|---|
| `api.log` | API application log and the **access log** (`app.api.access`: method, path, route, status, duration_ms, bytes, sse, client, principal, request_id) | 20 MB × 5 |
| `worker.log` | Scheduler, DAG runs, job queue | 20 MB × 5 |
| `jobs\<job_id>\<run_id>.log` | One file per job run (path also stored in `job_runs.log_path`, shown on Data Health and the System page) | Housekeeping deletes after 45 days |
| `filler.log`, `filler_tasks.log`, `ops\tasks\*.log` | Gap filler decisions, governor state, task output | 20 MB × 5 / housekeeping |
| `supervisor.log` | Child starts, exits, restarts, health checks | 20 MB × 5 |
| `app\<child>.out.log` | Raw stdout/stderr of each child (crash tracebacks land here) | Housekeeping copy-truncate above 50 MB |
| `app\supervisor.json` | Live status: children, pids, restarts, RSS | Rewritten every few seconds |
| `realtime.log` | Realtime supervisor and streams (when enabled) | 20 MB × 5 |
| `tray.log` | Tray app | 20 MB × 5 |
| `cli.log`, `export.log` | Manual CLI runs | 20 MB × 5 |

**Correlating a .NET call with the API log:**
1. Send `X-Request-ID` from the client. The sample's `RequestIdHandler` does this.
2. Search `api.log` for `"request_id": "<id>"`.
3. On a 5xx, the traceback is in the same file, under the same request id.
