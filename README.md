# BiG Equity 360

Global equity research platform: a Python backend (FastAPI + APScheduler worker) over a DuckDB / Parquet data lake, a React (Vite) dashboard, and a Windows tray app that supervises everything. Version in `VERSION`.

The repository holds code, contracts and configuration only. **The data lake (`data/`) is not in git**: the app rebuilds it from public and keyed sources with its own jobs. The only data files committed are the curated seed lists in `data/reference/seeds/` (universe lists, ADR ratios, manual overrides), which no job can regenerate.

## Quick start (Windows)

Requirements: Python **3.13**, Node.js **20+** (for the dashboard build), Git, ~50 GB of disk for the full universe (much less for the MVP universe).

```powershell
git clone <this repo> "BiG360"; cd BiG360
.\bootstrap.ps1                    # venv + locked deps, UI build, .env from template, seeds, doctor
notepad .env                       # FRED_APIKEY, Polygon_APIKEY, SEC_USER_AGENT (real e-mail) at minimum
.\BiG360.cmd                       # start: tray + supervisor; dashboard at http://127.0.0.1:8010
```

Options: `.\bootstrap.ps1 -FirstLoad` runs the first data load immediately in the console (nightly DAG on the MVP universe, price backfill, weekly DAG, serving DB build; hours). `.\bootstrap.ps1 -RegisterLogonTask` registers a Windows scheduled task so the background services start at every logon. Stop everything with `.\BiG360.cmd --quit`.

Linux / macOS / server: `docker compose up` (api, worker, frontend; mounts `./.env` and `./models_store`).

## What runs in the background

`BiG360.cmd` starts the tray (`app.desktop.tray`), which starts the supervisor (`app.ops.supervisor`) and its children:

| Process | Module | Role |
|---|---|---|
| API | `app.cli serve api` (port 8010) | REST API + built dashboard (`frontend/dist_app`) |
| Worker | `app.scheduler.worker` | runs the job DAGs from `contracts/jobs.yaml` on schedule (`config/schedule.yaml`, Europe/Lisbon), plus the job queue |
| Filler | `app.ops.filler` | gap detection and history backfill campaigns (`contracts/coverage.yaml`) |
| Studio lane | `app.modeling.lane` | model / study queue |
| Realtime | `app.cli realtime start`, quotes | live quotes and news (app mode) |
| Startup sync | `app.ops.sync run` | 90 s after the API is up, then every 30 min |

On an empty lake the worker's catch-up runs the missed nightly / weekly / monthly chain at start, the filler starts its history campaigns, and the API serves an empty snapshot (`data_status: pending`) until the first `build_serving_db` finishes. Models train themselves on the weekly chain; FinBERT downloads from Hugging Face on first use.

Developer mode instead of the tray: `.\run_all.ps1 [-Live]` (API on 8000, worker, Vite dev server on 5173). It doesn't start the filler or the startup sync, so a fresh lake fills more slowly that way.

## Data sources and keys

Required: `FRED_APIKEY`, `Polygon_APIKEY`, and `SEC_USER_AGENT` set to a real contact e-mail (SEC policy). Optional keys for regional sources are listed in `.env.example`; jobs whose key is missing are skipped with a `missing key` status. Keyless sources include SEC EDGAR, Yahoo, GDELT, ECB, GLEIF, CFTC, FINRA short interest, and several exchange / regulator feeds. Keys can also be stored in Windows Credential Manager. Check the setup any time with `cd backend; ..\.venv\Scripts\python.exe -m app.cli doctor`.

## Useful commands (from `backend/`)

```powershell
..\.venv\Scripts\python.exe -m app.cli doctor                     # environment / keys / disk check
..\.venv\Scripts\python.exe -m app.cli jobs status                # recent job runs
..\.venv\Scripts\python.exe -m app.cli jobs plan nightly --universe mvp
..\.venv\Scripts\python.exe -m app.cli jobs run-dag nightly --universe mvp
..\.venv\Scripts\python.exe -m app.cli db build-serving           # rebuild the serving snapshot
```

## Layout

`backend/` (app code and tests), `frontend/` (dashboard), `contracts/` (table schemas `schemas/*.yaml`, job registry `jobs.yaml`, coverage campaigns), `config/` (schedule, ops, sources), `docs/` (architecture, configuration, user guide), `integration/`, `packaging/` (installer, launcher, locked requirements), `scripts/`. Every lake table has a single owning job (docs/ARCHITECTURE.md, Ownership).

## External writers

Other local tools add research through the API rather than the lake: theses via `POST /api/v1/theses` (with `PATCH` and `/journal` for updates), watchlists via `POST /api/v1/watchlists`. Never write the Parquet tables or the DuckDB files directly; each has one owner and the serving snapshots are read-only.
