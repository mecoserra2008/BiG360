# Operations & Enterprise Roadmap (resumable plan)

**Requested by the user on 2026-10-19.** The app should:
- sustain its own data: live updates plus automatic detection and filling of gaps, run at the same efficiency;
- have every function working at 100% (Macro › Markets › Overview was named specifically);
- review and fix its flaws;
- run as a Windows application on the user's PC;
- refresh weekly, switchable to daily and later to intraday;
- be ready to move into the company's IT systems later, including integration with a C# / .NET managed network.

State lives in `data/state/ops_roadmap.json`. Tick the boxes below as work lands. **Execution starts only after the user says "go"**. The exception is the read-only audit (O2.1), which is already running.

---

## O1. Self-sustaining data operations: the "Data Ops supervisor"

**Goal:** the app knows what data it should have, finds what is missing or stale, and fills it in the background. Gap filling uses the same idempotent code as the live updates, under per-source rate budgets and a resource governor.

1. **Coverage registry** (`contracts/coverage.yaml`). One entry per dataset:
   - expected cadence, using calendars rather than fixed intervals: exchange trading calendars for prices, official release calendars for macro (already in `macro_calendar`), the CFTC Friday schedule, 13F deadlines, filing seasons;
   - the expected set of entities (universe, country list, market list) and the freshness SLA;
   - the source(s), with a backfill function and a live function;
   - priority and a cost per unit (requests, MB).
2. **Gap detector** job (`data_gaps` table). For each dataset it compares expected (calendar × entities) against actual, and classifies each gap:
   - missing;
   - stale;
   - partial (e.g. a GDELT day with 512/576 files);
   - suspicious (failed DQ, unit or FX glitch);
   - unfillable (source has no data; recorded so the detector stops retrying).

   Each gap records first_seen, attempts, last_error and status. Runs after every refresh and daily on its own.
3. **Gap filler**: a long-running, low-priority worker that drains `data_gaps` by priority.
   - **Rate budgets per source:** Polygon about 5/min, the Yahoo throttle pattern already built, SEC 10 req/s, GDELT bandwidth, and the official-API limits for FRED, Eurostat, ECB and others.
   - **Resource governor:** RAM cap (default 4 GB total), CPU priority BelowNormal, maximum concurrency. It pauses on battery, on a metered network, when RAM is low, and optionally when the user is active.
   - **Resumable**, with exponential back-off per gap.
   - **Same code as live updates:** the same ingestion modules and upserts, so backfill and live updates produce identical data.
4. **Fold existing one-off backfills into the filler:** GDELT E6, Yahoo price history, earnings dates, 13F, CFTC, ESEF/DART chains. No more hand-launched scripts.
5. **Data Health page** in the UI. This was parked idea #10; it now belongs here. It shows:
   - a dataset × freshness heatmap;
   - the gap queue and fill progress (e.g. "GDELT 2020–2024: 63% done, ETA 4 days");
   - source status and rate-budget use;
   - recent failures;
   - alerts on SLA breaches.
6. **Seed the known gaps from the backlog:**
   - GDELT 2025-05-13 partial; the rest of the 2020 backfill;
   - BaoStock (349 Chinese names on Yahoo only);
   - TWD FX; usd_per_unit gaps; FX scale glitches;
   - B3 pre-2005 units; Spain CNMV; HKEX board-meeting dates;
   - the 13F weekly out-of-memory risk.
7. **Unattended running:**
   - keep the PC awake during scheduled windows (SetThreadExecutionState), and catch up after sleep, which caused the 3-day loss earlier;
   - log rotation; disk-space guard; automatic clean-up of serving snapshots (keep the last 2).

## O2. Functionality at 100%: audit, fix, guard

1. **Read-only full audit** (running now): every route, tab and GET endpoint, plus data health, a reliability code review, and a Markets Overview deep dive. Output: `docs/AUDIT_2026-10-19.md`, with each finding graded P0–P3.
2. **Fix in waves:**
   - P0 broken, then P1 wrong or misleading, then P2 weak;
   - a **Macro › Markets › Overview redesign**, based on the audit (a portfolio-manager-oriented cross-asset morning view drawing on the new cross-asset, CFTC, earnings and smart-money data);
   - P3 cosmetic last.
3. **Regression guard**, so pages stay at 100%:
   - a scripted **smoke suite**: headless browser over every route and both themes; fails on console errors, empty panels or slow loads;
   - an **API contract sweep** of every GET with real IDs, checking shape and plausibility;
   - **golden data checks** for key numbers, e.g. AAPL revenue, SPX level, US CPI.

   The suite runs after each weekly refresh, and the result shows on Data Health.

## O3. Windows application

**Target:** `BiG360Setup.exe`. It installs and starts by itself, with no terminals, Node or manual commands.

1. **Single process tree:**
   - the frontend is built to static files (`vite build`) and served by FastAPI, so Node is not needed at runtime;
   - one **supervisor** process manages the API, the worker/scheduler and the gap filler, restarting each if it crashes.
2. **Packaging:**
   - an embedded Python runtime plus the locked dependencies (torch CPU, xgboost and others; about 2 GB; a portable distribution is more robust here than PyInstaller);
   - an **Inno Setup** installer giving Start-menu and desktop shortcuts, an uninstaller, and data kept in a chosen folder (the current `data/` is reused, not copied).
3. **Background mode:** a **Windows Service** (WinSW wrapper, runs without login) or a **tray app at login** (no admin rights needed). The tray menu offers Open dashboard, Pause/resume background jobs, Run refresh now, Data health and Quit.
4. **Schedule profiles** in one settings file and on a UI Settings page:
   - `weekly`: the default the user asked for. A full refresh Saturday morning (Lisbon time), chaining the nightly, weekly and monthly-when-due DAGs. The gap filler runs between refreshes, throttled.
   - `daily`: the current nightly cron, Tuesday to Saturday.
   - `intraday` (future): turns on the real-time layer (R1 bus already built; R2 live prices is deferred) and adds intraday pulls.

   Switching profile is one dropdown, with no code changes. The `jobs.yaml` crons become profile-driven.
5. **Secrets:** API keys move from `.env` into Windows Credential Manager (DPAPI). `.env` is still accepted as a fallback for development.
6. **Updates:** a versioned build plus an "update available" check from a local folder or share. A full auto-updater can come later.

## O4. Enterprise readiness & .NET integration

Staged so nothing done now has to be thrown away later.

**Stage A: now, on the laptop**
- Clean configuration and secrets.
- Structured JSON logs and `/health` plus `/metrics` endpoints.
- The **versioned OpenAPI contract** already exists (`contracts/api.yaml`, now 1.31.0, additive-only changes) and is the main integration asset.

**Stage B: department server**
- The app runs as a Windows Service on a company server, behind **IIS (ARR) or a .NET YARP gateway**, with corporate TLS certificates.
- **Identity:**
  - Entra ID (Azure AD) OIDC or Windows Integrated Auth;
  - FastAPI validates the bearer tokens, with roles (viewer / analyst / admin);
  - per-user watchlists, theses and alerts.
- **Shared state:**
  - `app_state` (watchlists, theses, studies, alerts) moves from a local DuckDB file to **SQL Server**, or PostgreSQL if that is the company standard;
  - the analytical store stays DuckDB/Parquet, which is read-mostly and fast;
  - optionally, the snapshot is published to SQL Server for BI and Power BI.
- Corporate outbound **proxy** support; scheduled backups.
- **Data licensing review. This is critical before company use.** SEC, FRED, Eurostat, ECB, CFTC and GDELT are fine. **Yahoo/yfinance is not licensed for commercial use**, and Polygon depends on the plan. A **vendor adapter interface** lets a licensed feed slot in as a price/fundamentals source: Bloomberg, LSEG/Refinitiv, FactSet, or the bank's own market-data service.
- Security: dependency scanning, an SBOM, SAST, and a pen-test checklist.

**Stage C: integration with the C#/.NET estate.** Python stays the analytics engine; .NET integrates with it. No rewrite is planned. Integration patterns, in recommended order:
1. **REST over the OpenAPI contract.** Generate a typed C# client with **NSwag or Kiota** from `api.yaml`, used through `HttpClientFactory` and **Polly** retries, with **MSAL** tokens. This has the lowest friction and is what most .NET teams expect.
2. **Event streaming.** The live SSE stream (`/api/v1/live/stream`) can be consumed by `HttpClient` streaming. For enterprise messaging, a bridge publishes the internal bus to **Azure Service Bus, RabbitMQ or Kafka**, whichever the company runs.
3. **Data-level access.** Snapshot tables are bulk-copied into SQL Server (`SqlBulkCopy`-compatible exports), or published as Parquet on a share and read with **Parquet.Net / DuckDB.NET**. This suits reporting and risk systems.
4. **gRPC** for low-latency service-to-service calls, only if a .NET service needs sub-10 ms calls.
5. **Hosting together:**
   - a .NET **YARP** gateway fronts the Python services, handling auth, routing and rate limits;
   - health checks feed the company's monitoring (SCOM, Azure Monitor, Grafana);
   - deployment through IT's tooling (SCCM/Intune, or containers if allowed).

**Deliverables for IT:**
- an architecture document;
- the OpenAPI contract and a change log (already kept);
- a **sample C# solution** (.NET 8 LTS) that calls the screener and Company 360, subscribes to the live stream and handles auth;
- an SBOM, a runbook (install, upgrade, backup, restore) and a data-licence matrix.

**Questions for the company IT team** (for later; nothing is blocked now):
- .NET version (8 or 9 LTS?);
- on-premises or Azure;
- AD or Entra ID;
- SQL Server version and standards;
- message bus;
- container policy;
- whether Python is allowed on servers;
- the outbound proxy;
- which market-data licences the bank holds;
- security review and change-management process.

---

## Execution order (proposed)
1. **O2.1 audit**: already running, read-only.
2. **O1 Data Ops supervisor**: detector, filler, Data Health page; existing backfills folded in.
3. **O2.2 fix waves**, including the Markets Overview redesign, then the **O2.3 regression guard**.
4. **O3 Windows app**: static frontend, supervisor, schedule profiles, tray/service, installer.
5. **O4 Stage A** now. **Stages B and C**: architecture document, C# sample client and IT questions now; the actual server move happens when the company is ready.

## Audit outcome (2026-10-19): changes to the plan
- **Step 0, before O1: restore freshness.**
  - Run a catch-up nightly for the 10-14..10-16 sessions and the missed weekly. Pause the GDELT backfill while it runs, for RAM.
  - Then run the worker with the scheduler ON (F01/F02).
  - Show the data-as-of date and scheduler state in the header and on the System page (F03/F13).
- **O1 additions:**
  - The serving rebuild must not hard-depend on analytics. Publish fresh prices even if an analytics job fails, marking the affected sections stale (F16).
  - Timed-out thread jobs must be killed and retried (F18). Fix `ref_universe` (F17).
- **Before the Markets Overview redesign, fix data integrity:**
  - ticker-reuse stitching (SPCX/BNY/FIG, F04);
  - the missing 10-12 non-US bars (F05);
  - the snapshot mixing regions at different dates (F06);
  - 100× unit flips.
- Full list: `docs/AUDIT_2026-10-19.md` (2 P0 · 16 P1 · 16 P2 · 8 P3 · R1–R10).

## O2.2a Data integrity (audit F04 / F05 / F06 / F15 / F32 + backend P1/P2 labels)
State: `data/state/ops_roadmap.json` key `o2_integrity`. Code: `app/normalize/price_integrity.py`, `app/analytics/beneish.py`, `app/api/data_integrity.py`; api.yaml 1.29.0.
- [x] F04 identifier-reuse detector (`detect_identity_breaks`) + universe scan
- [x] F04 guard wired into `prices_finalize` (`build_prices`) + overrides seed `reference/seeds/price_identity_breaks.csv`; re-finalized all regions 2026-10-20 (121 series split, SPCX/BNY/FIG fixed)
- [x] F05 root cause (Yahoo 10-13 morning fetch returned no 10-12 bar for non-US markets and stored live partial 10-13 bars; no nightly ran after)
- [x] F05 calendar session check `app.normalize.price_integrity.missing_price_sessions` (for the Data Ops gap detector) + `refill_missing_sessions`
- [x] F05 live-bar guards (`drop_unfinished_bars` in yf_prices.normalize, `drop_unfinished_staged` in finalize) + EU/UK 10-12..10-19 refill (1,669 symbols)
- [x] F32 unit-flip repair (`repair_unit_scale`) in prices_finalize (incl. FX 10x flips); DQ rule `large_cap_jumps` (warn)
- [x] F06 explicit `data_as_of` per region / index / sector / mover / curve in the API; 1D sector aggregates aligned per region
- [x] F15 Beneish components + recalibrated model-based screen (ratios rows + API, badge beneish_flag only for the screen)
- [x] F07/F08/F09/F10/F12 regime scope + true driver units/z, credit spreads panel, macro tile transforms, curve policy-rate marker
- [x] api.yaml 1.29.0 + CONTRACT_CHANGES + tests
- [ ] Downstream recompute (technicals, valuation, scoring, ML features, serving) -> next `run-dag nightly --resume` (orchestrator)
- [ ] Not done: euro IG OAS (no official free source on FRED/ECB SDW found), EUR pre-1999 / ISK / IDR usd_per_unit history

## O2.2b UI fixes + Markets Overview redesign (audit UI findings; frontend only)
State: `data/state/ops_roadmap.json` key `o2_ui`. Code: `frontend/src/pages/market/MarketMacro.tsx` + `frontend/src/pages/market/overview/*`, `frontend/src/api/adapters/market.ts`, `frontend/src/api/client.ts` (marketOverview, Beneish), generated types regenerated for api.yaml 1.29.0. No backend change.
- [x] F06 data-as-of per region (header chips, "mixed dates"), index / mover / sector / curve dates; degrades to benchmark-index dates on APIs < 1.29
- [x] F07 regime: region scope sent as is (only when the running API's openapi lists it), "No regime model for {region}", labelled GLOBAL fallback, no Expansion default; one-line chip linking to /macro + details popover
- [x] F08 drivers as level in their true unit + (z vs model fit); never "z" on a level
- [x] F09 credit: `credit_spreads[]`, 1D / 1M (calendar) / pctile (window), 1y spark, Baa−10Y labelled yield spread; legacy payloads show no fabricated 1M / percentile
- [x] F10 macro tiles: display value / unit / label, observation date, calendar spark window; F40 duplicate-concept dedupe (official source kept)
- [x] F11 index board (level, 1D/1W/1M/YTD, as-of, 2σ marker), breadth per region, movers (liquid-only filter, F21 client side)
- [x] F12 / F20 curve: policy rate as a dotted marker, numeric log/linear tenor axis, curves < 2 market tenors hidden and 2-tenor curves flagged, dropdown selector, 2s10s / 5s30s / 10Y 1M / as-of chips, US 2s10s 1y history, policy pricing table (approximation label)
- [x] F19 sector heatmap: winsorised colour scale (95th pct), sector − index option, constituent counts / dates in tooltip and x-axis, null-cap sectors left out of the treemap with a note
- [x] Overview redesign: header (as-of + risk-on/off gauge + regime chip), index board, breadth, cross-asset tape (rates, FX, commodities, VIX, crypto, credit), sectors + movers, rates + credit, policy path + macro headlines
- [x] F15 Company 360: `beneish_flag` = warning, `beneish_watch` = info, model-based Beneish breakdown (components, contributions, growth-neutral M, red flags, corroboration)
- [x] F35 duplicate React key (xgboost_rank and ensemble both mapped to "xgboost-63"): keyed on model_run_id + horizon, raw model type labelled
- [x] F36 Company 360 tabs renamed (News & Sentiment, Earnings, Event risk, Network & Geo) and wrap instead of scrolling off-screen
- [x] F22 System freshness table grouped per dataset (worst status, freshest / stalest region), prices / FX / macro first; per-region view kept
- [x] F23 live strip says "Not live · last heartbeat …" when the realtime supervisor is stopped
- [x] F41 cockpit "Latest releases": newest-captured age + warning badge
- [x] F37 USER_GUIDE port note (:8010) and page table
- [ ] Re-verify on the next published snapshot (API 1.29) — render paths verified with injected 1.29 fields only

## O3 Windows application (implementation 2026-10-20)
State: `data/state/ops_roadmap.json` key `o3`. Code: `backend/app/desktop/*` (tray, tray_actions, credentials, autostart, updates, appconfig), `backend/app/api/static_ui.py` (+ 1 hook in `app/api/main.py`), `app/ops/supervisor.py` (`--app`, `--serve-ui`, credentials, `--no-schedule`), `app/ops/worker_schedule.py` (refresh-now request), `frontend/.env.app` + `npm run build:app`, `packaging/` (build.ps1, build_launcher.ps1, make_icon.py, launcher/, installer/BiG360.iss, requirements.lock.txt), `config/app.yaml`, `VERSION`, `BiG360.exe` (project root). User decisions: weekly Saturday 08:00 Lisbon (O1 profiles), tray at login (no service).
- [x] O3.1a Static frontend: `vite build --mode app` → `frontend/dist_app` (same-origin `/api/v1`, no mock, no CORS), served by FastAPI at `/` with SPA history fallback when `BIG360_SERVE_UI=1`; `/api/v1`, `/docs`, `/openapi.json`, `/health` unchanged; Vite dev on :5173 unchanged
- [x] O3.1b Supervisor app mode: `--app` / `--serve-ui` (default on in app mode), API keys from Credential Manager to the children, `--no-schedule`; single instance, STOP-file stop and crash back-off reused from O1
- [x] O3.3 Tray app (pure ctypes Shell_NotifyIcon, no new dependency): open dashboard, status, run refresh now, pause/resume (filler.PAUSE), schedule weekly/daily (PUT /system/schedule), data health, logs, start at login (HKCU Run), update available, quit; toast notifications for new ops alerts and refresh results; starts / restarts the supervisor
- [x] O3.2a Packaging: `packaging/build.ps1` (lock from .venv, icon, UI, app files, portable Python 3.13 runtime `copy` (offline) or `embed` (python.org embeddable + pip --target), launchers, zip, installer, publish); `BiG360.exe` launcher stub (csc, .NET Framework 4) + `.vbs` / `.cmd`
- [x] O3.2b Inno Setup script `packaging/installer/BiG360.iss` (per user, data folder page → `{app}\.env` BIG360_DATA_DIR, models_store junction, shortcuts, autostart task, key-migration task, graceful stop on upgrade / uninstall; data kept)
- [ ] O3.2c Compile `BiG360Setup.exe`: needs Inno Setup 6 (ISCC.exe) — not installed (user decision)
- [x] O3.5 Secrets: Windows Credential Manager (`BiG360/<key>`, ctypes CredRead/CredWrite), `python -m app.desktop.credentials migrate|verify|status|set|delete|exec`; `.env` dev fallback unchanged
- [ ] O3.5b Run the one-time key migration on this PC (user: `python -m app.desktop.credentials migrate`)
- [x] O3.6 `VERSION` + "update available" check against a folder / share (`config/app.yaml` update_dir, `latest.json` or `BiG360Setup-<ver>.exe`)
- [x] Docs: USER_GUIDE §7 (install, first run, tray menu, schedule, data / logs, uninstall, dev mode)
- [x] Tests: static serving / SPA fallback, credentials (test target prefix), tray handlers, supervisor app mode, refresh request; vite build + tsc clean; app-mode smoke on :8095 (isolated data dir, headless Chrome)
- [ ] O3.7 Switch-over of the running processes to the tray app (orchestrator / user; steps in the O3 report)

## O2.2c Remaining backend findings (audit F13 / F14 / F21 / F24-F34 / F39 / F42 + contract gap)
State: `data/state/ops_roadmap.json` key `o2c`. api.yaml 1.30.0, jobs.yaml v30.
- [x] F13 header badge follows the per-section `serving_sections` status (prices failed / missing = error, stale market section = stale, stale analytics = chip only)
- [x] F14 freshness: forward-looking tables judged on the last ingest (no negative lag); stale when the writer's latest run failed (routers/system.py)
- [x] F21 movers screened server-side (`liquid`, ADV / cap / USD price floors, unknown cap only with 4x ADV, `movers_limit` 25) + UI uses the server screen
- [x] Contract gap: `BeneishDetail.corroboration` documented
- [x] F24 silent skips: Eurostat geo errors kept per series (macro_eu), Form 4 identity failures / unmapped CIKs in the run message (sec_form4), XBRL submissions errors counted (sec_xbrl), CFTC lake read error fails the report type instead of a full re-download (cftc)
- [x] F26 earnings dates: no more 1900-01-01 marker rows (fetch log in data/state/earnings), legacy rows purged on the next earnings_dates_fetch
- [x] F27 regime models EU / JP / CN / KR (HMM, official Eurostat / ECB / OECD / IMF / BIS series, model-based) - first fit at the next macro_regime_fit
- [x] F28 Model Lab: fold windows / Sharpe read from model_runs.fold_metrics; ensemble runs persist fold metrics + labelled inherited importance; production gate enforces the leakage-check rule (a demoted transformer could be promoted)
- [x] F30 payloads: earnings calendar `offset` / `compact`, backtest run `series=weekly|none`
- [x] F33 A-share listings (XSHG / XSHE) of A+H issuers in region CN (serving)
- [x] F39 Latin-script primary names (full-width folded, CJK names -> Latin alias), local name in name_local (serving)
- [ ] F25 fundamentals coverage CN / KR / TW (needs OPENDART key; BaoStock IP ban) - user decision
- [ ] F29 13F CUSIP mapping: 81% of 2025-26 value mapped; 32.7k unmapped CUSIPs (mostly non-universe instruments) need an OpenFIGI batch (keyless ~2.3 h) or a key - user decision
- [ ] F31 TWD daily rate (CBC) - new source; BGN: no BGN-quoted securities (moot)
- [ ] F42 US "confirmed" earnings dates - no official free source (Nasdaq scraping not allowed by the data policy)
- [x] Found by the regression guard: earnings_reactions had been overwritten with 82 rows (a run saw a partial earnings_events scan); restored (296,033 rows) + shrink guard in app.earnings.reactions; earnings_signals re-run
- [x] Found: production gate had left the demoted ft_transformer as 1M production; gate now enforces the leakage-check rule (ml_ensemble re-run: xgboost_rank production for 1M / 3M / 6M)
- [x] Recompute run 2026-10-20 evening: macro_regime_fit + nowcast (6 scopes), macro_recession, ml_ensemble, composite_ml_blend, earnings_dates_fetch (11,052 sentinels purged), earnings_reactions, earnings_signals
- [ ] build_serving_db + API restart (orchestrator) to publish them

## O2.3 Regression guard
State: `data/state/ops_roadmap.json` key `o23`. Code: `backend/app/qa/` (api_sweep, golden + golden.yaml, smoke + smoke_routes.yaml, regression), `scripts/smoke_ui.py`.
- [x] UI smoke suite (headless Chrome over DevTools, every App.tsx route + tab, both themes; console errors, failed requests, error / empty panels via data-testid, load > 8 s, horizontal overflow; read-only) -> data/reports/smoke/<run>/
- [x] API contract sweep (every GET of the served OpenAPI with real ids; status, schema, NaN / inf, future as-of, SLA from coverage.yaml, served vs contract version)
- [x] Golden data checks (backend/app/qa/golden.yaml: AAPL / MSFT revenue, MSFT shares, AAPL cap, SPX / Nikkei, US CPI y/y, EURUSD, USDJPY, UST 10Y, unemployment)
- [x] Job `qa_regression` (nightly, soft-depends on alerts_eval: inside the weekly chain right after the snapshot is published), CLI `python -m app.qa`
- [x] GET /system/qa + Data Health "Regression guard" panel + ops alert `qa_regression`
- [ ] First scheduled run observed (after the API restart on 1.30)

## O4 Enterprise readiness & .NET integration (implementation 2026-10-20)
State: `data/state/ops_roadmap.json` key `o4`. api.yaml 1.31.0. Code: `backend/app/api/observability.py`, `backend/app/api/auth.py`, `backend/app/core/config.py` + `logsetup.py` (+ hooks in `app/api/main.py`, `app/cli.py`), `backend/app/ingestion/vendors.py`, `backend/app/ops/export.py`, `scripts/make_sbom.py`, `scripts/openapi_31_to_30.py`. Docs: `docs/CONFIGURATION.md`, `docs/it/*`. Sample: `integration/dotnet/BiG360.Client.Sample`.

**Stage A hardening**
- [x] A1 `/health` liveness (unchanged) + `/ready` readiness (200/503: snapshot published, worker heartbeat ≤ 300 s; filler informational)
- [x] A2 `/metrics` Prometheus text, dependency-free: request counts + latency histograms per route template, SSE connections, snapshot age, data age per table/region, dataset status, job outcomes (7 d, last success, last status), worker / filler / governor / gap queue / campaign progress
- [x] A3 Structured JSON logs: request-id middleware (`X-Request-ID` in/out), JSON access log in `data/logs/api.log`, `service` + `request_id` + fields in every JSON line (api, worker, filler, supervisor, realtime, tray, cli), optional JSON on stderr
- [x] A4a Settings reference `docs/CONFIGURATION.md` (every env var, `.env` key, config and contract file, with defaults)
- [x] A4b Gateway-ready settings: CORS allow-list (comma/JSON, regex, credentials), `BIG360_ROOT_PATH`, proxy headers / forwarded IPs
- [x] A4c `BIG360_AUTH` (default `none`) + OIDC/JWT bearer validator (Entra issuer / audience / JWKS discovery, roles claim → viewer / analyst / admin), PyJWT[crypto] if installed else built-in RS256 verifier; tests with a local RSA key + JWKS
- [x] A5 api.yaml 1.31.0 (additive: `/ready`, `/metrics`, `Readiness`, `securitySchemes.bearerAuth`), CONTRACT_CHANGES, version assertion, `test_all_operations_covered` green
- [x] A6 Live API runs 1.31.0 with the final observability code (the API child was restarted by the supervisor at 22:02 UTC after exiting with code 1 during a heavy UI sweep; /ready, /metrics and route-template labels verified live)

**IT integration pack (`docs/it/`)**
- [x] B1 `ARCHITECTURE.md` (components, process tree, data flow, storage + sizes, scheduling, gap filler, security model, ports, footprint; Mermaid)
- [x] B2 `INTEGRATION_DOTNET.md` (REST + NSwag/Kiota + HttpClientFactory/Polly/MSAL, SSE + Service Bus/RabbitMQ/Kafka bridge, Parquet/DuckDB.NET/SqlBulkCopy, YARP, gRPC, versioning rules)
- [x] B3 `DEPLOYMENT_SERVER.md` (WinSW service / containers, YARP or IIS/ARR, TLS, Entra ID / AD, app_state → SQL Server table mapping + migration, backups, monitoring hooks, proxy, sizing)
- [x] B4 `DATA_LICENSING.md` (licence matrix per source; Yahoo ⛔, Polygon plan-dependent, official sources OK with attribution; vendor adapter interface `app.ingestion.vendors` + stub/file-drop adapter)
- [x] B5 `SECURITY.md` (threat model, secrets, SBOM `docs/it/sbom.json` CycloneDX 1.5 = 380 components, scanning recommendation, data classification)
- [x] B6 `RUNBOOK.md` (install, start/stop, upgrade, backup/restore, troubleshooting from real incidents, log locations)
- [x] B7 `QUESTIONS_FOR_IT.md` (16 decisions with recommendations)
- [x] Extra: snapshot export for SQL Server / BI (`python -m app.ops.export`, CSV/Parquet + manifest + T-SQL DDL); OpenAPI 3.1 → 3.0 down-converter for NSwag

**C# sample (`integration/dotnet/BiG360.Client.Sample`, net8.0)**
- [x] C1 `BiG360.Client` thin typed client (health, ready, contract version, system status, search, Company 360 overview, screener, market overview, earnings calendar, SSE stream + resuming subscriber), bearer handler (off by default), request-id handler, Polly v8 DI registration, MSAL provider (opt-in), README with NSwag / Kiota commands
- [x] C2 Console demo (health, screener, company lookup, SSE with cancellation, retry demo)
- [x] C3 Verified with the .NET 6 SDK (`-p:Big360TargetFramework=net6.0 -p:Big360Offline=true`) and run read-only against the live API on :8010
- [ ] C4 net8.0 build with Polly / HttpClientFactory / MSAL packages: needs the .NET 8 (or 10 LTS) SDK + NuGet (user decision)

## Perf pass (2026-10-20, after QA 20261020T213011Z)
State: `data/state/ops_roadmap.json` key `perf`. Tools: `scratchpad/perf/profile_page.py` (CDP network log per page) and `scratchpad/perf/replay.py` (replays the captured API calls).
- **Root cause of the slow and "cold" pages: browser connection starvation, not the API.** During the QA run the API log shows no slow data request. Each full page load opens an EventSource (`/live/stream`). Documents kept in Chrome's back/forward cache kept their stream open for about 60 s. Once 5–6 were held, Chrome's HTTP/1.1 limit of 6 connections per host queued every other request of the new page until a stream was evicted. This reproduced on a warm API: 13 sequential navigations gave 52 s stalls on /macro/cb/fed and /events. Fix in `frontend/src/api/live/store.ts` (`bindPageLifecycle`):
  - `pagehide` closes the stream; a bfcache restore (`pageshow` with `persisted`) reopens it;
  - a tab hidden for more than 60 s releases the stream and resumes it from the last event id when shown again. This also protects users with several app tabs open.
- **API cold start.** The full-table warm-up (`deps._warm`) read every column of the 6.5 GB snapshot into DuckDB's buffer pool, which had no memory cap (default 80 % of RAM). The API sat at about 7 GB RSS, and the warm-up kept DuckDB's shared worker pool busy for about 25 s after startup, so the first requests took 11–20 s.
  - Replaced by `app/api/warmup.py`. It replays the main pages' API calls (91 requests: shell, /macro, then the main pages) in-process through the ASGI stack after startup and after each snapshot switch. It runs one request at a time and waits while user requests are in flight, with a 180 s budget. It stops on a snapshot switch or a 401/403. It is off with BIG360_API_WARMUP=0, with auth=oidc, and under pytest. It also pre-imports the Studio export modules.
  - New `BIG360_API_DUCKDB_MEMORY` (default 4GB) caps the serving connection.

| Page | Before: first load / retry (QA 213011Z) | After: QA 222449Z (dark / light) |
|---|---|---|
| /macro/positioning/067651 | 30.2 s (timeout) / 11.5 s | 0.63 / 0.62 s |
| /macro/calendar | 30.1 s / 9.5 s | 0.62 / 0.62 s |
| /screener | 30.1 s / 8.4 s | 0.65 / 0.67 s |
| /macro/cb/fed | 23.9 s cold | 0.64 / 0.62 s |
| /sectors | 22.1 s cold | 0.78 / 0.67 s |
| /smart-money | 23.9 s cold | 0.62 / 0.62 s |
| /events | 26.3 s cold | 0.89 / 0.68 s |
| /models | 30.1 s cold | 0.62 / 0.62 s |
| /system | 30.1 / 19.1 s cold | 0.86 / 0.87 s |

- **QA 20261020T222449Z:** smoke passed 148/148 (no warnings; median 615 ms, max 1.04 s); golden 12/12; API 143 passed, 2 warnings.
  - The two API warnings are live alerts pending (as before) and the first Studio export at 3.5 s, which was a lazy import; the warm-up now pre-imports it (0.2 s after a restart).
- **API restart**, browser first visit right at readiness:
  - before: /macro 3.3 s, then the startup stall described above;
  - after: /macro 1.15 s; every following page 0.6 s from response-cache hits;
  - warm-up finishes 11–20 s after startup;
  - API memory: 6.7 GB working set / 8.3 GB private before, 0.9–1.1 GB / 2.4–2.6 GB after.
- **/health is a constant-time liveness check** (`main.py` + `deps.peek_snapshot` / `deps.dq_blocking_cached`).
  - Before: a sync handler in the threadpool. It read the whole `dq_results` table through `dq_summary()` on every call and could wait on the snapshot-switch lock. During the old warm-up's startup contention it took 10–30 s, and the supervisor's 10 s timeout with 3 strikes then restarts the API.
  - Now: `async` on the event loop, no DuckDB query, never waits on the lock (returns the previous snapshot while a switch is opening). The dq flag comes from a cache refreshed in a background thread every 30 s.
  - Measured during a 16-worker replay of every page's calls:
    - old code, warm API: median 109 ms, p95 231 ms, max 1.04 s;
    - new code right after a restart, during the warm-up: median 8 ms, p95 72 ms, max 0.70 s;
    - new code, warm API: median 7 ms, p95 58 ms, max 0.34 s.
  - API memory after that load plus the warm-up: 1.3 GB working set / 3.0 GB private. It is bounded by the 4 GB DuckDB cap, 8 DuckDB threads and the 256 MB response cache. The warm-up yields to user traffic: under the load it took 85 s instead of 11–20 s.
- The API restarts at 22:02 / 22:17 / 22:31 / 22:40 UTC were made on purpose by this pass (the API child was killed for the supervisor to restart it). The exit code 1 at 22:02 noted under O4 A6 was one of these restarts. The exit at 22:23:25 UTC was not from this pass. Because the supervisor counts each kill as a failure, its restart backoff has grown to 300 s; it resets after 600 s of healthy running.
- No serving rebuild needed. No contract change.
- Follow-ups (not done):
  - /openapi.json (515 KB) is re-rendered on every full page load and every API call waits on it; it could be pre-rendered with an ETag;
  - /live/news groups the whole news table on each call (200–450 ms, shell call);
  - an HTTP/2 front end, or one shared stream across tabs (BroadcastChannel), would remove the 6-connection ceiling for good.

## Progress
- [x] O2.1 Audit report (docs/AUDIT_2026-10-19.md)
- [ ] O0 Restore freshness (catch-up nightly + scheduler on + as-of display)
  - [ ] O0.1 Catch-up nightly 10-12.. + weekly (scripts/ops_catchup.ps1, orchestrator)
  - [x] O0.2 Scheduler state on /system/status (on/off, profile, next run + last completed run per DAG) and System page panel (F01/F03)
  - [x] O0.3 Header: data as-of (per region on hover), "Scheduler off" / "Data stale" chip; optional live_* tables no longer turn the badge into "Error" (F13)
  - [x] O0.4 Ops alerts: scheduler off (> 1 day = error), refresh overdue, SLA breaches, disk, filler (F01 / R1)
  - [ ] O0.5 Switch-over to the supervisor with the scheduler ON (user / orchestrator; see the O1 report)
- [x] O0/O1 reliability fixes
  - [x] F16 build_serving_db / dq_checks soft dependencies + `serving_sections` staleness table (R2)
  - [x] F18 thread timeouts: cooperative cancel, then JobKilled injection, retried; process jobs killed with their tree; prices_global_yf / gdelt_stream / earnings_dates_fetch / yf_fundamentals on the process executor (R3/R4)
  - [x] F17 ref_universe: missing-key source (DART) is an info, ragged seed CSVs tolerated; price / news / GDELT jobs soft-depend on it
  - [x] Schedule profiles weekly (default) / daily / intraday placeholder, PUT /system/schedule, hot reload, catch-up rule, keep-awake + wake timer (O3.4)
- [ ] O1 Data Ops supervisor
  - [x] O1.1 Coverage registry contracts/coverage.yaml
  - [x] O1.2 Gap detector (job data_gaps_detect -> ops/data_gaps)
  - [x] O1.3 Gap filler with resource governor + per-source rate budgets (python -m app.ops.filler)
  - [x] O1.4 Campaigns folded in: GDELT E6 (adopted / same parameters), 13F history, CFTC history, earnings chain, Yahoo price history
  - [x] O1.5 Data Health page (/system/data-health)
  - [x] O1.6 Known gaps seeded (PHASE3_TODO + audit)
  - [x] O1.7 Keep-awake, log rotation, disk guard, snapshot clean-up (keep 2)
  - [x] O1.8 Supervisor entry point (python -m app.ops.supervisor)
  - [ ] O1.9 Live switch-over + first unattended weekly run observed
- [ ] O2.2 Fix waves + Markets Overview redesign
  - [x] O2.2a data integrity (backend)
  - [x] O2.2b UI fixes + Markets Overview redesign (frontend; re-check on the 1.29 snapshot pending)
  - [x] O2.2c remaining backend findings (open: F25 / F29 / F31 / F42 need keys or decisions)
- [x] O2.3 Regression guard (code; first scheduled run pending)
- [ ] O3 Windows application + schedule profiles (built and tested; installer compile + switch-over pending, see "O3 Windows application" above)
- [ ] O4 Stage A hardening + IT integration pack (architecture doc, C# sample, licence matrix): code, docs and sample done, live API on 1.31.0; .NET 8 build pending (see "O4 Enterprise readiness" above)

## Pause checkpoint: 2026-10-20 ~14:00 (user at lunch)
Everything is stopped: the supervisor (API, worker, filler), Vite, and the O2.2c/O2.3 agent.

**State**
- **Snapshot:** `serving_R20261020T1220105SP0` (as_of 2026-10-19), api.yaml 1.30.0. Repaired prices, analytics recomputed, weekly XGBoost trained.
- **Supervisor:** ran 13:35–13:56 in dev mode. Scheduler ON with the weekly profile (next run Sat 24 Oct 08:00 Lisbon). It caught up `cftc_weekly`.
- **BUG, filler:** every tick crashed with `OSError [Errno 22]` on the task file `data/state/ops/tasks/gap_prices.CN_*.json`, because the task id contains `*`. So **no gap was filled and E6 did not resume.** E6 is still at 2024-09-29, with 1,728 days left. Fix: sanitise task ids before using them as filenames (backend/app/ops/filler.py / tasks.py), then add a test.
- **O2.2c/O2.3 agent:** stopped mid-way. Code is done for most findings; the state file key `o2c` shows `code_done_pending_recompute`. Pending:
  - an F26 run;
  - the F27 regime fit for EU/JP/CN/KR;
  - F28, which needs `ml_ensemble` / the next weekly run;
  - F33/F39, which need a serving rebuild;
  - the first `qa_regression` run.

  Resume the agent via SendMessage, or re-run the listed jobs.
- **Japan prices:** still as_of 2026-10-13. The filler's refill is blocked by the bug above.

**On "continue"**
1. Fix the filler task-id bug and test it.
2. Resume the O2.2c agent so it finishes and reports the jobs to run.
3. Run the pending jobs, then one serving rebuild.
4. `npm run build:app`, then start **BiG360.exe** (app mode, tray). Check tray › Status.
5. O4: the IT integration pack (architecture doc, C# sample once .NET 8 is approved, licence matrix).

**Open user decisions:** Inno Setup 6 install; credential migration into Windows Credential Manager (`python -m app.desktop.credentials migrate`/`verify`); the .NET 8 install.

## Refresh pipeline fixes 2026-10-26
Goal: the weekly refresh completes unattended on this PC (32 GB RAM, 20 threads). Profiles, probes and logs: `scratchpad/refresh_fix/` (prof_baseline.out = old code, prof_new2.out = new code, build_live.out = the live run).

**1. build_serving_db: OOM after 3-4 h (stale snapshot serving_R20261022T2056130T13, data 2026-10-19)**
- Where the limit came from: not the governor. The build used the generic job connection (`BIG360_DUCKDB_MEMORY`, default `12GB`; DuckDB reads GB as 10^9 bytes = 11.18 GiB = the "11.1 GiB/11.1 GiB used"), 16 threads and preserve_insertion_order = true.
- Root cause: every failed attempt died in `ev2_stories`, one GROUP BY story_id over the 15.6 M story-days of the 400-day window (news_stories grew with the un-pruned recent months + CC-NEWS / E6 merges) with list-valued arg_max / list() aggregates (urls, domain_ids, countries, issuer lists), which cannot spill. Before it, the grown lake made the rest slow: the prices dedupe window over ~66 M rows (QUALIFY row_number + ingested_at), two blockwise nested-loop joins in `sec_base` (LEFT JOIN conditions with OR / coalesce), `QUALIFY max() OVER (PARTITION BY ...)` over full technicals / factor tables, a second descending window in px_stats, the 5-log10 float32 rounding in every lake view.
- Fixes (`app/api/serving.py`, `app/core/config.py`, `app/core/storage.py`):
  - own connection `serving_build_connection()`: `BIG360_SERVING_DUCKDB_MEMORY` 7GB, `BIG360_SERVING_DUCKDB_THREADS` 12, spill dir `BIG360_SERVING_TEMP_DIR` (default data/tmp/duckdb_serving, max 60 GB), preserve_insertion_order = false (every ordered table has an explicit ORDER BY);
  - ev2_stories: fixed-width pre-pass per story (superset of the final filter), single-day stories (98 %) projected straight from their row, only the ~183k multi-day stories aggregated; same rows (test against the old query);
  - prices: no per-key dedupe window (PK unique by construction; only securities with bars in two regions are deduped), `year` partition pruning, 8 ordered security slices; prices_w in 4 slices; fund in 4 issuer slices;
  - sec_base: fv / eps matched with hash joins (was 87 of its 130 s in nested-loop joins); last split date without a window; px_stats with one ascending window;
  - latest-per-key tables (tech levels / zones / level stats / setups, peer, fv, forecasts, macro betas, tp_*, tm_setups, tv3_*) via `_latest_sql` (hash aggregate + semi join);
  - `sql_round_sig7` uses round(x, k) (log10 once; bit-identical on all 98.5 M prices rows, 1.5-2x faster);
  - per-section and per-table (>= 5 s) timing + process memory in the job log; `serving sections:` summary line;
  - the build runs alone in the exclusive heavy slot (item 5), timeout 120 -> 60 min.
- Numbers: see the table below.

**2. prices_finalize 2 h 53 min (full rebuild every run)**
- Root cause: since O2.2a every run rebuilt every region from the full 130 M-row staging (98.5 M output rows, ~100 s of Polars work per million rows plus a ~1-min staging scan per 1,500-security chunk) and rewrote every partition.
- Fix (`app/normalize/prices_incremental.py`, `prices.finalize(mode=auto|full)`): per-region watermark (data/state/prices_finalize.json; bootstrapped from prices_daily max(ingested_at) - 6 h); dirty securities = staging rows ingested since (15-min overlap); tail re-build from 200 calendar days before the first new bar; full-history re-build for new securities, revisions > 45 days back, override / universe-row changes and when the identity-break or unit-repair guard (or an unresolved 10^k jump) fires in the tail window; factor rescale of the stored history for actions that go ex in the tail (new dividends, pending dividends whose previous close moved); stitch-guard decisions always from the full staging history (decision pass); shared staging scans for all regions; tail / finish phases in 2 parallel threads; only touched (region, year) partitions merged; mode=full kept for back-fills (`--param mode=full --param timeout_min=240`). Timeout 150 -> 60 min.
- Tests: `tests/normalize/test_prices_incremental.py` (auto run == full rebuild with a tail dividend + a history revision, idempotent re-run, no-op run; unit flip in the new week escalates to a full re-build and is repaired; unfinished-bar guard; plan rules; factor rescale; stitch decisions in a window).

**3. event_daily_agg duplicate keys (2026-05-22..31) and event_changepoints "duplicate labels"**
- Root cause: the day-file store replaced a day in two steps (purge the month file, write the d- file; compaction: write the month file, unlink the d- files). A Windows reader holding a file made the second step fail (PermissionError in the 2026-10-20 gdelt redo log: `os.replace(... m-202605.parquet)`), so the same days lived in both files; `compact_month` (no PK check) and `read_range` concatenated both copies, and `recompute_z` (events_agg / backfill full z run) wrote them back. Not an "append" writer: every writer already went through write_day.
- Fix (`app/events_v2/store.py`, `agg.py`, `events_models/common.py`, `bocpd.py`): d- file written first, month purge second, renames / unlinks retried; a d- file is authoritative for its day in read_range and compact_month; compactions dedupe on the primary key; `heal_overlaps()` runs at the start of events_agg (DQ PkUnique stays the blocking guard); load_t1 dedupes; event_changepoints aggregates one row per day per stream. Contract write_mode append -> upsert (documentation of the day-replace semantics). Live check: 0 duplicate keys in event_daily_agg.
- Tests: `tests/events_v2/test_store_day_replace.py` (8).

**4. Timeouts: event_hotspots 60 min, conformal_envelopes 45 min**
- event_hotspots: a back-fill (E6 / redo + the full z recompute) changes the fingerprint of hundreds of weeks; the nightly run then did up to 400 weeks x themes x measures x 499 permutations as a thread job (its process pool kept running after the timeout). Now: time budget `max_minutes` 25 (newest weeks first, rest on the next runs), process executor, timeout 40.
- conformal_envelopes: ~25k securities = ~70-80 CPU-min of single-threaded numpy per run (the job was sized for ~4 min), and each retry restarted at the first region. Now: regions in 2 worker processes, a checkpoint per finished region (a retry resumes), timeout 90. Test: `tests/conformal/test_envelope_resume.py`.

**5. Scheduling**
- weekly_refresh: Saturday 08:00 -> **02:00** Lisbon (config/schedule.yaml + built-in default; catch-up and wake timer unchanged; hot-reloaded by the worker).
- Exclusive heavy slot `app/ops/heavy_slot.py` (jobs.yaml `resource: exclusive` on build_serving_db): the build announces itself and waits (<= 60 min) until no heavy (process) job runs; heavy jobs wait while it runs (<= 90 min); news_hourly / news_finalise are deferred to the next hour; the gap filler yields (pseudo-DAG `serving_build`: no new task, heavy tasks stopped). Markers in data/state/ops/heavy/ carry pid + expiry (a crashed owner never blocks). `BIG360_HEAVY_SLOT=0` disables. Runner: `params.timeout_min` overrides a job's timeout for one run. Tests: `tests/ops/test_heavy_slot.py`.

| | Before | After |
|---|---|---|

## Alert hygiene 2026-10-30
Four alerts on Data Health / System were wrong or never cleared. Probes and logs: `scratchpad/alerts_fix/`. Contract 1.43.0 (additive). Tests: `backend/tests/ops/test_child_exit.py` (8), `test_alert_hygiene.py` (11); `test_incident_1024.py`, `test_qa_regression.py`, `test_api_serving.py` updated; vitest `src/api/ops/__tests__/ops.test.ts`, `src/lib/__tests__/availability.test.ts`.

**1. Realtime child "stuck in its exit"**
- The 07:27 zombie (pid 30740, 2,814 MB) was not the realtime child: the pid had been reused. On 10-26 30740 was the realtime venv launcher (gone at 03:35). On 10-30 it was the sync's `confluence_zones` job process, caught by a reaper sweep 0.2 s after the job ended and gone by the next sweep. That was a normal teardown, not a hang.
- What the realtime child really did (10-26 02:41, 10-29 23:07): it logged "realtime supervisor stopped", stayed alive for more than 35 s, and was killed after the 60 s grace. The cause was the interpreter shutdown. `asyncio.run()` joins the default-executor threads (up to 300 s, then without a limit at exit), and the spawn parse pool's manager thread is joined too. Both times, cancelled `to_thread` calls were blocked: HTTP retries while DNS was failing, and the GDELT parse waiting on the pool for up to 600 s. Torch was not the cause: FinBERT in a worker thread exits in 0.9 s, or 0.2 s with `os._exit`.
- Fix (`app/ops/shutdown.py`, realtime `supervisor.py` / `cli.py`, `streams/gdelt.py`, `quotes/service.py`):
  - a STOP-file watcher thread (0.5 s);
  - a bounded loop close: own executor, shut down without a join; the parse pool's process is killed;
  - after cleanup, flush the logs and stdio, then `os._exit`;
  - an exit deadline (realtime 45 s, quotes 22 s) hard-exits if the cleanup itself blocks;
  - the parse child ignores console CTRL+C / CTRL+BREAK.
- Supervisor: realtime and quotes get the STOP file only, never CTRL+BREAK (verified by a test). After the grace their Job Object is terminated. A member still in its exit blocks the next start for up to 90 s (`EXIT_TEARDOWN_S`); only after that is it a zombie. The reaper reports a zombie only after 120 s in exit (two sweeps), and names its owner (child or `sync`).

**2. "Datasets stale beyond SLA" vs the sync**
- `app/ops/freshness.py` gives one verdict per dataset.
  - The source is the sync's reason code when the sync scanned within 6 h; otherwise it is the gap detector's verdict (`stale`).
  - The detector's lag / SLA is used only to time `due` and `source_unavailable`.
  - Alerts fire for `failed`, for `due` or `source_unavailable` past the SLA, and for `stale`. Nothing else alerts; Data Health shows the reason, next expected date, retry time or key names instead.
- Data Health rows, `sla_breaches` and the `sla_*` alerts all use it, so the two views can no longer disagree.

**3. "Last nightly run failed" never clearing**
- `refresh_failed` (replaces `nightly_failed`): it names the jobs whose latest attempt failed, across nightly, weekly, monthly, sync and manual runs.
- `last_refresh` is the most recent of the sync and the scheduled runs; `refresh_overdue` uses it.

**4. Stale "Regression guard failed"**
- `qa_regression` now also runs at the end of every sync that published a snapshot (`suites=auto`). The API sweep and golden checks run every time. The UI smoke runs at most once every 24 h, and not while a heavy job holds the slot or free RAM is under 4 GB.
- The state is merged per suite (run id and time per suite).
  - A failure that a newer run did not re-check is `stale` / `superseded_by` (warn).
  - Past its horizon (api / golden 26 h, smoke 50 h) it is info only.
  - The panel shows each run's age.
- QA run 2026-10-30 07:40Z: api warn (167 passed, 2 warn = live alerts / quotes pending), golden 12/12, smoke 162 / 164. The 2 failures were the Ownership tab of 7203.T, where the FINRA short-interest panel was an empty-data state for a non-US listing. The 13F, short-interest and score panels now use `NoDataState` (components/ui/missing.tsx). It shows a neutral "not applicable" note (`data-testid="na-state"`) only when the catalog or the security documents `not_applicable`; any other gap is still an empty-data state.

Go-live: one app restart (tray Quit, start BiG360.exe) after `npm run build:app`. The supervisor, realtime, quotes, API and worker all load the new code.

## Worker memory & ref_universe fixes 2026-10-31
Two production issues: the worker / startup-sync process reached 13-18 GB during the nightly / weekly DAGs with
~0.1 GB free RAM, and the nightly `ref_universe` blanked the sector of ~4,300 US issuers until `identity_resolve`
restored them. Probes, samples and replays: `scratchpad/ram_fix/` (`samples_before.csv` = 2 s machine / process
sampler for the whole day, `replay_*.out`, `table.md`, `ref_dry.json`). jobs.yaml **v42**, api.yaml **1.45.0**
(additive), sector_taxonomy.yaml v3.

**1. Worker memory: root causes (measured)**
- The runner was instrumented first: every attempt now records peak working set / private / Job Object commit,
  start / end memory and the machine's lowest free RAM (job log `mem ...` line, `data/state/ops/job_mem.jsonl`).
- Thread jobs ran inside the long-lived worker / sync interpreter and the interpreter kept what they freed. Replayed
  one at a time in one process (`replay_dag.py before` = the worker minus parallelism): +2.6 GB kept after
  identity_resolve, +3.2 after fundamentals_normalize (6.2 GB private after two jobs), +3.5 after prices_finalize,
  +4.9 after ratios_kpis. With up to 6 such jobs side by side (worker_max_jobs 6, no memory rule, every job DuckDB
  connection at 12 GB / 16 threads, Polars one thread per core) the sync tree reached 23-36 GB private / 11-16 GB
  working set with min free RAM 0.01-0.18 GB in every run of 10-30/31; the startup sync of 10-31 07:19 hit 0.00 GB
  at 08:23:59 (ml_features alone: 13.3 GB private in 23 s).
- Eight jobs drove the peaks, each with an avoidable full-table pattern:
  - prices_finalize (incremental): stitch-guard decisions for 4,000 securities per part, each part holding their FULL
    staging history -> 19.5 GB; the plan phase materialised every stored bar of a region before aggregating.
  - fundamentals_normalize: every 40-issuer batch re-read and re-wrote whole tables (all fiscal-year partitions of
    fundamentals_canonical = 15.8 M rows, all 8 fundamentals_wide partitions), 72 times per run -> 13-15 GB.
  - ratios_kpis: every result held to the end (2,800 issuers x all vintages), Executor.map computing ahead -> 12.2 GB.
  - ml_features: region lookup over security_id / region of all 99 M prices_daily rows -> 13 GB (OOM at a 6 GB cap;
    the panel's securities only: 2.1 GB, 1 s).
  - identity_resolve: price-life group_by over all 99 M rows in memory (> 7 GB; streaming: 1.8 GB, 4 s).
  - regime_performance: `sort_by(date).last()` per (security, month) since 1995 is not streamable -> 10 GB.
  - dq_checks: one DuckDB connection at 12 GB -> 9.2 GB.
  - tech_levels / confluence_zones: 10 spawned pool workers, each with a 20-thread Polars pool -> 18-20 GB commit for
    5-7 GB of working set (impossible under any commit cap).
- Admission knew nothing about memory: no budgets, the only guard was "process jobs wait while free < 3 GB".

**2. Fixes**
- `app/ops/winsys.py`: `JobObject.set_memory_limit()` (JOB_OBJECT_LIMIT_JOB_MEMORY, KILL_ON_JOB_CLOSE kept),
  `limit_info()` (cap, PeakJobMemoryUsed), `memory_detail()`; `release_memory()` (gc + HeapCompact on every process
  heap + CRT `_heapmin`; optional EmptyWorkingSet; malloc_trim on glibc).
- `app/ops/ram_budget.py` (new): budgets (`mem_gb`; default 0.5 GB thread / 2 GB process); cross-runner reservations
  in `data/state/ops/ram/` (worker, sync and CLI runs see each other; dead owner / expired = ignored); admission
  `free - outstanding reservations - headroom (BIG360_RAM_HEADROOM_GB, 4) >= budget`; per-job caps (`job_env`:
  DuckDB memory_limit 60 % / serving build 70 % of the budget, DuckDB / Polars / OpenMP / MKL / OpenBLAS threads
  ~1.5 per GB, 2..12, `BIG360_JOB_MEM_GB`); `pool_workers()` / `small_worker_threads()` for job-internal pools.
- `app/scheduler/runner.py`: admission in the DAG loop (a job waiting for RAM lets others pass for 10 min, then only
  jobs that waited as long may start; a job alone - nothing reserved by any runner - starts at once under its cap,
  waiting cannot free RAM that is not ours; light thread jobs never wait when nothing of the run is in flight).
  Process jobs get the caps in their own environment (set at CreateProcess, before polars is imported) and their Job
  Object gets the budget as committed-memory cap. A job at >= 98 % of its cap is killed and reported
  `MemoryLimitExceeded: OOM-killed at X GB (budget Y GB, Job Object memory cap)` (also when it died by itself with
  MemoryError / an allocator abort above 80 % of the cap); it is retried once with jobs.yaml `oom_params` (smaller
  chunks), never re-run as is. Thread jobs give memory back when they end (gc + heap compaction while no other thread
  job runs). A process job stopped by a shutdown is reported `JobCancelled`, not a timeout. Dry-run plans show
  `mem=`. Default `worker_max_process_jobs` 2 -> 4 (the budgets decide).
- `app/scheduler/worker.py`, `app/ops/worker_schedule.py`: when the worker goes idle after a schedule entry or a
  queue request it releases memory and trims its working set.
- `app/ops/sync/graph.py`: process jobs with `mem_gb` < 3 GB use the sync's light lane (heavy lane stays 1).
- Job code (same results, bounded memory; equivalence tests below): prices_incremental (stitch parts of 300
  securities `STITCH_PART_SECURITIES`, following `chunk_securities`; streaming index aggregation), fundamentals
  (`_replace_rows` one partition at a time, flush every `flush_issuers` = 400), ratios (`flush_rows` 2 M upserts,
  bounded look-ahead, stale sector_kpis rows removed when an issuer changes sector), ml.features (panel securities
  only), identity price_life (streaming), macro.markets.returns (max(date) + join, streamable), technicals.parallel
  (pool workers with 2 threads, worker count from the budget), events_models hawkes / hotspots (pool size from the
  budget, 1-thread workers), scoring_full (`chunk_issuers`).
- jobs.yaml v42: 49 jobs with `mem_gb` (budget = 1.4 x the commit peak measured on 10-31, 0.5 GB steps). Moved from
  thread to process: prices_finalize, ref_universe, identity_resolve(_weekly), fundamentals_normalize / _rebuild,
  ratios_kpis, valuation_daily, insider_signals, gdelt_company_daily, sentiment_daily, earnings_events / reactions /
  expectations, cross_asset_signals, regime_performance, scoring, event_escalation / gpr / exposure / changepoints,
  events_retention, story_cluster, cb_tone, conformal_coverage, dq_checks, build_serving_db,
  serving_sections_publish and the weekly sec_xbrl_bulk, sec_fsds, dart_bulk, moat, ml_labels. `oom_params` on
  prices_finalize, fundamentals_normalize / _rebuild, ratios_kpis, tech_levels, confluence_zones,
  technicals_probabilities, conformal_envelopes, scoring_full. Still threads: network fetchers and jobs measured
  below ~1 GB (macro_*, cb_* except cb_tone, events_agg, event_connectedness, earnings_signals, composite_ml_blend,
  macro_regime_nowcast, alerts / thesis / gaps). Budgets of story_cluster, cb_tone, event_changepoints,
  events_retention, ref_universe and the weekly bulk loaders are estimates (not replayed, ref_universe dry run 2.7 GB):
  check `job_mem.jsonl` after the first Saturday.

**3. Numbers (replays on live data, 10-31)** "Before" = old executors in one long-lived process, one job at a
time (thread figures = growth of the shared process). "After" = v42 budgets, caps and admission, DAG parallelism.
Run while the startup sync was held off (refresh-DAG lock) and the gap filler paused; heavy slot on.

| job | before: peak RSS GB | before: kept after the job GB | after: peak RSS GB (commit) | mem_gb v42 |
|---|---|---|---|---|
| prices_finalize | thread +19.5 | 3.5 | 1.3 (1.8) in the replay; 8.1 (9.5) for a 10-h tail of 7,500 stitch securities | 10 |
| fundamentals_normalize | thread +13.0 | 3.2 | 4.4 (5.2) | 7.5 |
| ml_features | process 9.7 (13.0) | - | 1.2 (1.5) | 3 |
| ratios_kpis | thread +12.2 | 4.9 | 0.3 (0.6) (small increment) | 6 |
| regime_performance | thread +10.0 | 0.2 | 5.0 (6.3) | 9 |
| dq_checks | thread +9.2 | 0 | 4.1 (4.6) | 6 |
| identity_resolve | thread +7.2 | 2.6 | 4.6 (4.9) | 7 |
| confluence_zones | process 6.6 (19.8) | - | 5.3 (6.6) | 9 |
| tech_levels | process 5.2 (18.4) | - | 4.3 (5.0) | 7 |
| cross_asset_signals | thread +4.9 | 0.9 | 5.1 (5.9) | 8.5 |
| forecast | process 5.4 (7.5) | - | 4.6 (6.0) | 8.5 |
| build_serving_db | ~8.5 (refresh fixes 10-26) | - | 7.1 (7.7) | 9 |
| gdelt_company_daily | thread +3.3 | 3.2 | 3.8 (4.4) | 6.5 |
| insider_signals | thread +3.2 | 1.8 | 3.7 (5.1) | 7 |
| technicals_probabilities | process 4.1 (6.2) | - | 3.4 (4.6) | 6.5 |
| conformal_envelopes | process 4.0 (7.3) | - | 3.5 (5.0) | 7 |
| earnings_events | thread +2.0 | 1.2 | 2.9 (3.7) | 5.5 |
| scoring_full | process 3.3 (5.1) | - | 2.2 (3.5) | 5 |
| ml_predict / ml_predict_full | process 2.8 (4.7) / 2.3 (4.3) | - | 2.4 (3.1) / 2.3 (3.1) | 5 / 5 |
| 24 smaller jobs | 0-2.1 each | up to 1.2 each | 0.1-2.4 each | 3-6 or thread |

Full per-job table: `scratchpad/ram_fix/table.md`. Thread "before" figures exclude job-internal process pools
(event_escalation's 10 hawkes workers were invisible to the process measurement).

| machine free RAM (GB) | min | 5th pct | median |
|---|---|---|---|
| before: live startup sync 10-31 08:09-08:34 (old code) | 0.00 | 4.26 | 8.25 |
| before: replay identity_resolve + fundamentals_normalize | 0.00 | 2.08 | 8.33 |
| before: replay prices_finalize | 0.01 | 0.04 | 13.10 |
| before: replay of 37 other derive jobs | 1.54 | 6.96 | 10.00 |
| after: replay of the 42 derive jobs (runs A + B) | **1.22** | 2.51 | 6.28 |

- The largest job fell from 19.5 GB to 7.1 GB (the serving build); no job commits more than 9.5 GB; the worker /
  sync interpreter no longer keeps job memory (a process job returns everything at exit).
- The 4 GB free target was **not** met in the after replay: min 1.22 GB at 14:12 (confluence_zones 3.9 GB + the
  worker's hourly news job 2.1 GB - the worker still ran the old code, so it did not see the replay's reservations)
  and 1.39 GB at 14:45 (build_serving_db alone, 7.3 GB). Between 13:00 and 15:00 this PC had only 5.4-8.2 GB free
  with no job of ours running (VS Code, browsers, TradingView, Defender and the OS held ~24 GB), against 13-16 GB in
  the morning. Under that baseline any job above ~4 GB working set takes the machine below 4 GB; the morning baseline
  leaves room for every budget. If the target must hold in the evening too: build_serving_db `mem_gb` 6 (DuckDB 4.2
  GiB, more spill, slower; not replayed) or closing desktop apps before the Saturday 02:00 refresh.
- Run A (first budget estimates) OOM-killed insider_signals at 4.92 GB (cap 5 GB) and event_escalation at 4.01 GB
  (cap 4 GB, its pool) - cleanly, with the reason in job_runs and nothing else affected. Budgets were then re-derived
  as 1.4 x measured peak and the affected part re-run (run B): no OOM; all jobs succeeded except dq_checks, which
  failed on its own data checks (section 5).

**4. ref_universe sector wipe**
- Cause: `build_reference` fetches SEC submissions (SIC) only for the top ~1,500 US candidates plus seeds and
  yfinance info for ~800 plus seeds, rebuilds every issuer row from that and overwrites `issuers`. Every other issuer
  came out with `sic_code` null and `classification_source` unknown, i.e. no sector. `identity_resolve` re-fetched
  their SIC (30-day cache) and re-classified them, at most 3,000 CIKs per run. On 10-31, 4,306 US issuers had their
  sector only from that back-fill: 2,908 were restored at 03:16, the rest only by identity_resolve_weekly at 06:52.
  Side effects: scoring / serving jobs between 02:42 and 03:16 (and on weekdays, until the weekly run, ~1,400 issuers
  beyond the budget) saw missing sectors, and a wipe + restore could change a sector (Henry Schein HLTH -> INDU,
  yf_industry -> sic), leaving sector_kpis rows of 4 issuers in two sector partitions (dq sector_kpis/pk_unique 111).
- Fix (`app/normalize/identity.py`): before classifying, inputs the build did not fetch (sic_code, yf_sector,
  yf_industry) are carried from the current snapshot (a freshly fetched value wins); after classifying, an issuer whose
  result is `unknown` keeps its previous sector / subsector / source (deliberate exclusions such as SIC 6770 and any
  real new classification win). Merge, never blank out.
- DQ: `sector_loss(prev, new)` on every build -> `data/state/ref_universe_sectors.json`; above 1 % the job is
  `partial` with a warning and an error log; dq hook `issuers/sector_loss` (severity error, 8-day horizon); ops alert
  `sector_loss` on System / Data Health (api.yaml 1.45.0, additive).
- Live dry run of the new build (write=False, 2.7 GB commit): 0 of 20,696 classified issuers lose their sector
  (3,913 issuers get their inputs carried over); the old code blanked ~4,300 per night.
- Repair: the 02:42 wipe had already been restored by identity_resolve (03:16) and identity_resolve_weekly (06:52).
  sector_taxonomy v3 maps SIC 3712-3713 / 3750-3759 (autos) and 7600-7699 (consumer services): 12 more issuers
  classified (`scratchpad/ram_fix/repair_sectors.py`). US issuers still without a sector: 368 = 280 SPACs (SIC 6770,
  excluded by design), 80 BDCs / closed-end funds (SEC has no SIC for them), 8 others; HK 2,433 (the HKEX list has no
  industry; separate gap). sector_kpis: 375 stale rows of 4 issuers removed (`repair_sector_kpis.py`).

**5. Issues found outside this task**
- dq_checks fails on data: institutional_holdings / short_interest freshness 12.5 d, news_stories / story_members
  pk_unique (6 / 10), ratios pit_violation 0.00105 (> 0.001; 0.00098 before the 09:46 recompute).
- ml_labels (weekly) fails: 7,656 duplicate primary keys [date, security_id, horizon].
- build_serving_db in the 10-31 weekly run failed 3x on `lake.peer_scores_ext` (the worker still held the pre-1.44
  serving module; the restart below fixes it).
- A stray `C:\Python313\python.exe -` (pid 78048, 14 MB) from an interactive session waits on stdin.

**Tests:** `tests/platform/test_ram_budget.py` (18: admission rule, reservations / stale markers / child-tree usage,
runner parallelism limited by budgets with memory-allocating fake jobs, waiting for another runner's reservation,
alone-start, two starving jobs, Job Object cap refusing an allocation, OOM kill + one retry with smaller chunks, OOM
without chunk params not retried, process-job peak record, thread-job memory release, pmap / pool sizing, sync lanes),
`tests/normalize/test_ref_universe_sector_merge.py` (5; the merge test fails without the fix),
`tests/normalize/test_fundamentals_replace_ram.py`, `tests/analytics/test_sector_kpis_partition_move.py`. Re-run:
platform/test_runner, test_dag, test_dq, normalize/test_identity, test_fundamentals, test_taxonomy,
analytics/test_ratios, ops/test_sync_run, test_incident_1024, test_worker_schedule, test_heavy_slot (one timing test
flaky under load, passes alone), macro_v4/test_engine, test_contracts.

**Go-live:** restart the app once (tray Quit, start BiG360.exe) so the worker loads jobs.yaml v42 and the new runner
and the API serves api 1.45.0. The startup sync already runs the new code (fresh process per sync; the 14:51 sync
did). After the next Saturday refresh check `data/state/ops/job_mem.jsonl` (`oom_killed`, peaks vs `mem_gb`) and
`sync_history.jsonl` `ram.min_machine_free_gb`. Switches: `BIG360_RAM_BUDGET=0` (admission off),
`BIG360_JOB_MEM_CAP=0` (no Job Object cap), `BIG360_RAM_HEADROOM_GB`, `BIG360_RAM_WAIT_MAX_S`,
`BIG360_THREAD_RELEASE=0`.
