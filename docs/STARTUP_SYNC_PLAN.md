# Startup sync, live fetchers and data completeness (plan)

**Requested by the user on 2026-10-27:**
- Every time the app is opened (BiG360.exe), it finds what is missing from the latest data. It fetches only that, in parallel, in the background.
- All data points are available in every part of the platform, and the live fetchers are running.
- A data point that is not available yet is skipped with a reason. The app never breaks because of it.
- Every data topic has a plan.

**Status:** the plan is approved in principle. Execution has started.

## 1. Startup sync engine ("what's missing → fetch only that → publish")

Runs in the background on every app start (supervisor start), then every 30 minutes while the app is open.

1. **Freshness scan.** For every dataset in `contracts/coverage.yaml`, compute the **expected latest period** from real calendars, then compare it with what is stored. That gives a **delta** of entities × dates.
   - **Calendars used:** exchange sessions per region, the official macro release calendar, the CFTC Friday release, 13F filing deadlines, FINRA settlement dates, the earnings calendar, GDELT 15-minute slots.
   - **Watermarks used:** per dataset, per source and per entity.
2. **Reason code for every gap.**

   | Code | Meaning | Action |
   |---|---|---|
   | `fresh` | Up to date | none |
   | `due` | Data exists at the source and is missing here | fetch |
   | `not_yet_published` | The release or session hasn't happened yet: macro release date in the future, market still open, filing deadline not reached | skip, show the next expected date |
   | `source_unavailable` | The source is down or rate-limited | back off, retry later, show when |
   | `no_source` | No free or official source exists (e.g. DE/IT shorts, TWD daily) | skip permanently, documented |
   | `needs_key` | An API key is missing (OPENDART, KRX, BANXICO, Companies House) | skip, show which key unlocks it |
   | `failed` | The job errored | retry with back-off; after N failures, park it and alert |

3. **Minimal plan.**
   - Only jobs whose outputs have a `due` gap run, each with **incremental parameters** (`since` = watermark, entity lists).
   - Derived analytics run only if their inputs changed. Job `run_key`s and fingerprints already exist.
4. **Parallel executor.**
   - **Network-bound fetchers** (prices per region, macro sources, filings, news, CFTC, FX) run **in parallel**, capped per source by the rate budgets.
   - **CPU / RAM-heavy steps** (finalise, analytics, ML) run in order under the RAM governor and the exclusive heavy slot.
   - Everything stays inside the Job Objects, so nothing leaks.
5. **Never break.**
   - Every dependency is soft unless it is truly required.
   - A failed or skipped input marks only its own **serving section** stale.
   - Everything else publishes.
6. **Incremental publish.** Rebuild only the serving **sections** whose inputs changed, then hot-swap them, so there is no full 30-minute snapshot build on every start. The full build is kept for the weekly refresh.
7. **Visible progress.** A startup "Syncing data…" chip in the header shows progress: datasets checked, due, fetched, skipped (with reasons). A **Data Sync** panel on Data Health shows each dataset's status, reason and next expected update. Tray toast: "Data up to date as of …".

## 2. Live fetchers (while the app is open)

| Feed | Cadence | Source | Notes |
|---|---|---|---|
| News (RSS + GDELT) | 1 min | already live | keep |
| **Delayed quotes** for watchlists, indices, FX, futures, open positions | 1–5 min during market hours per region | Yahoo/Polygon (personal use) | header ticker plus live price on Company 360, Instruments and Watchlist; never written into the official daily bars |
| **SEC filings** (8-K, Form 4, 13D/G) | 2–5 min | EDGAR "latest filings" Atom feed | triggers earnings, insider and activist updates plus alerts |
| **Official macro releases** | at the release time (from the calendar) | FRED/ECB/Eurostat/BLS… | fetches the series right after the scheduled release; surprise computed immediately |
| CFTC | Fri 15:30 ET (+ poll) | CFTC | |
| Earnings releases | on 8-K 2.02 / TDnet / HKEX | existing | surprise update |

## 3. Data completeness: "all data points available in all parts"

- **A coverage audit across every page and API field.** For each field that is null or missing for an entity, assign a reason from §1:
  - not published;
  - no source;
  - needs key;
  - pipeline bug;
  - mapping gap, e.g. a FINRA ticker that doesn't resolve.
- **Fix every pipeline bug and mapping gap.** Known ones:
  - `si.pct_float` empty in BQL;
  - the smart-money z-score is constant;
  - ROIC outliers;
  - JP prices stuck at 10-13;
  - 17% FINRA symbol resolution;
  - SEC acceptance times 4–5 h late;
  - ASML ROIC.
- **The UI shows the reason instead of a blank.** "n/a" gets a tooltip, e.g. "Not published yet: next release 2026-11-05" or "No free source for this market".

## 4. Per-topic plan

| Topic | Expected-latest rule | Fetch strategy | Skip reasons |
|---|---|---|---|
| Prices: US, EU, UK, JP, KR, HK, CN, LATAM, APAC | last completed session per exchange calendar | Polygon grouped daily (US) + Yahoo batched per region since watermark, in parallel; incremental finalise | market not closed yet (`not_yet_published`); KRX key (`needs_key`); BaoStock blocked (Yahoo fallback) |
| FX | last ECB/FRED fixing | ECB + FRED H.10 incremental | TWD weekly lag (documented) |
| Macro (FRED, Eurostat, ECB, OECD, IMF, national) | per-series release calendar | fetch only series whose release date has passed since the last observation | next release date not reached; series discontinued |
| Fundamentals / filings (SEC, ESEF, EDINET, DART, TDnet, HKEX, CVM, FCA) | new filings since the watermark | index first, then only new filers' facts | OPENDART key; source down |
| Insiders, 13F, 13D, FINRA SI / FTD, EU shorts | per source schedule (Form 4 daily, 13F quarterly + 45 d, SI bi-monthly) | incremental | not due |
| CFTC | latest Tuesday report released Friday | incremental | not released yet |
| Earnings dates and reactions | calendar plus releases | incremental | provider-estimate labels |
| News / GDELT | 15-min slots / live | live service + hourly compaction; history through the filler | outage days (`unfillable`) |
| Analytics: technicals, conformal, techprob, meta, factors, scores, ML features, smart money, supply chain | when inputs change | incremental recompute of affected securities | upstream not fresh, marked stale |
| ML models | weekly | weekly refit | |
| Serving | when any section changes | section-level incremental publish | |

## 5. Execution
- **Agent A, startup sync engine:** freshness scan, reason codes, minimal plan, parallel executor, section publish, UI and tray.
- **Agent B, live fetchers:** quotes, SEC live filings, macro release watcher, plus completeness of live UI elements.
- **Agent C, completeness audit and fixes:** every page and field gets a reason; fix the known pipeline and mapping bugs.
- Go-live is one app restart. After that, every BiG360.exe start catches up automatically.

## Progress
- [x] A Startup sync engine (2026-10-30; go-live = `npm run build:app` + one app restart, see "Workstream A status" below)
- [x] B Live fetchers (2026-10-27; go-live = one app restart, see "Workstream B status" below)
- [x] C Completeness audit + fixes (2026-10-29; go-live = one API restart + build:app, see "Workstream C status" below)

## Workstream B status (live fetchers, 2026-10-27)
- **Delayed quotes:** new 5th supervisor child `quotes` (`python -m app.cli realtime quotes`, app.realtime.quotes). It is separate from the realtime child because that child runs at 1.3-1.5 GB with FinBERT and its 1536 MB cap restarts it every 1-2 h; the quotes child uses about 180 MB, with a 512 MB cap. Each child has its own Job Object and stop file. Tray toggle: "Live quotes on/off".
  - Universe: watchlists, open theses, indices, FX majors, futures front months, cockpit assets, and securities viewed in the UI (about 155 securities).
  - Source: Yahoo batched quotes, 50 symbols per request, through the shared `yfinance` bucket, max 400 requests/h. Polygon snapshot is used only if the plan allows it; the free plan is disabled with a reason.
  - Cadence: every 60 s while an exchange is open, plus a 25-min closing-print tail; FX and CME Globex every 120 s. Closed markets idle with the reason and the next open.
  - Output: data/realtime/quotes.sqlite (labelled delayed, never written to prices_daily), bus topics `quotes.tick` / `quotes.session`, GET /live/quotes.
  - UI: Company 360 header, Watchlist, Instruments, Markets Overview index board, and the LIVE strip ticker plus the quotes / news chips.
- **SEC live filings:** the existing EDGAR Atom stream (30 s on US business hours, 120 s otherwise; shared 8 req/s limiter) already feeds the earnings 8-K 2.02, insider and new_filing alert consumers. A new `activism` consumer turns Schedule 13D/G into `signals` and evaluates `activist_13d` alerts live.
- **SEC acceptance-time fix:** see app.ingestion.sec_acceptance. EDGAR serves acceptanceDateTime late by the ET offset per filer file. The parsers now correct it at 97.8% on 1,105 labelled filings, against about 40% for face value. Stored rows were corrected once: 3,447 filings_index rows and 397,535 filing_news rows. A sample of 75 checked against the SGML headers was 75/75 correct.
- **Macro release watcher:** the existing calendar-driven `macro_capture` stream captures each release (actual, model-based surprise, `macro` type `release`) and the alert consumer fires `macro_surprise`. The next releases are now exposed on the Live feeds panel.
- **Data Health > Live feeds:** per feed, last poll, latency, items/hour, requests/hour, errors and reason (GET /live/feeds). Contract version 1.40.0.
- Agent C: the "SEC acceptance times 4-5 h late" item of section 3 is done by B.

## Workstream C status (data completeness, 2026-10-29)
- [x] Coverage audit of every BQL field (816) per universe x region on the live API, plus a page-API sweep (Company 360, technicals, conformal for 20 securities in 10 regions; 22 page endpoints). Report: docs/DATA_COMPLETENESS.md; machine-readable: contracts/field_availability.yaml (`python -m app.qa.completeness --write`).
- [x] Reason codes = section 1 codes plus `mapping_gap`, `pipeline_bug`, `stale`, `not_applicable`, `out_of_scope`, `suppressed`, each mapped to a sync code (app.core.availability.REASONS; contract AvailabilityCode).
- [x] Fixed: si.pct_float (float proxy), FINRA / FTD symbol resolution (class shares, ETFs, SC market class, ADR rows on the US line, ticker reuse), smart-money robust z, ROIC (currency basis, Yahoo invested-capital chain, plausibility bounds + flags), ratios / valuation for every issuer with statements (jobs.yaml v40), factor delta_1m, US SIC sectors, KSIC / CVM names, revenue per employee, smart-money overview joins, BQL duplicate name / sort(n). JP prices: no longer stuck.
- [x] UI: `<Missing>` / `<Avail>` / `<AvailabilityScope>` tooltips on Company 360 (header, overview, earnings, ownership, technicals), Screener, Smart Money, Earnings calendar, Macro tiles, Cross-asset. API 1.42.0: GET /meta/field-availability, GET /companies/{id}/availability.
- [x] Lake recomputes done (2026-10-28, heavy slot): 1,001 issuers re-normalised, ratios_kpis rebuild universe full, valuation_daily universe full, smart_money_signals, identity short-interest re-resolution.
- Remaining gaps with reason and plan: docs/DATA_COMPLETENESS.md "Remaining gaps".


## Workstream A status (startup sync engine, 2026-10-30)
Code: `backend/app/ops/sync/` (policy, expect, scan, graph, run, publish, trigger, state, api, `python -m app.ops.sync scan|run|status|request|stop`). State: data/state/ops/sync_status.json, sync_ledger.json, sync_history.jsonl, sync_publish.json; lock data/.locks/dag_sync.lock. Work state: data/state/startup_sync.json.
- [x] **Freshness scan** (scan.py, expect.py). Every coverage.yaml dataset plus one `job:<id>` row per daily source job that no dataset covers (29). The stored side comes from the gap detector's checks (watermarks / max dates per region, currency, source). The expected side is computed to the minute: exchange closes + publish delay (a market still open, or closed less than the delay ago, is not expected yet), ECB fixing 16:00 CET on TARGET days, macro releases by scheduled time + 2 h lag, CFTC Friday 15:30 ET, 13F deadline, FINRA publication (+8 sessions, 18:00 ET), GDELT day + 40 min, daily cadence for source jobs. Output: reason per dataset and per entity (`fresh`, `due`, `not_yet_published` + next_expected, `source_unavailable` + retry_at, `no_source`, `needs_key` + key names, `failed` + attempts) and a delta plan with incremental params (Polygon dates, Yahoo symbols of the due regions with a 5d window, BaoStock / TWSE start, B3 look-back, macro sources, GDELT days). A metered network turns every download into `source_unavailable`.
- [x] **Ledger.** After each pass the sync re-scans. A period still missing after a successful fetch becomes `not_yet_published`, with back-off 1, 2, 4, 8, 12 h, parked for 24 h after 6 tries. A failed job becomes `failed`, or `source_unavailable` when the error is network-like (also a partial run with a network symptom), with back-off 30 min to 8 h, parked after 3 failures with an ALERT note. A job skipped for a missing key becomes `needs_key`.
- [x] **Minimal plan + parallel executor** (run.py, graph.py). Pass 1 (core): the planned fetchers and the headline analytics. Publish. Pass 2 (extended): daily source jobs, slow fetchers (BaoStock, GDELT, SEC facts) and the model analytics. Publish again.
  - Network fetchers run in parallel (8 at once, per source at most the runner's SOURCE_MAX_JOBS: one Polygon, one Yahoo, three SEC). Heavy steps (process executor or HEAVY_JOBS) run one at a time, after the machine guard. Light derive jobs share 3 slots.
  - Derive jobs run only if one of their input tables changed after their last successful start (input fingerprint), with per-job minimum intervals for noisy inputs.
  - Everything goes through the JobRunner (job_runs rows with dag_run_id `D...-sync`, job locks, process jobs in Job Objects, heavy slot, timeouts).
  - Dependencies are ordering-only except HARD_DEPS (ml_features -> ml_predict -> blend, tech_levels -> confluence, macro_cells -> cycle, cftc_cot -> signals).
  - A failure marks only its own serving section (serving_sections uses the job's latest status).
  - Concurrent-write file races are re-queued once, alone on their tables. Jobs whose last success took more than 80 % of their timeout get 1.5x (at most 180 min). Long jobs are not retried inside a sync: the ledger retries them on the next one.
- [x] **Section-incremental publish** (publish.py, job `serving_sections_publish`, exclusive slot). The section map is captured by running every builder section on an empty lake through a recording connection: lake inputs, written and read snapshot tables, as_of dependence, SQL hash. A section is dirty when an input changed after the snapshot's build start, its code hash changed, the as_of moved (as_of-dependent sections), or it reads or co-writes a dirty section's tables. Publish modes: none; incremental (copy CURRENT, rebuild the dirty sections + meta, verify, flip CURRENT, which the API hot-swaps); or the full build when the dirty sections cost more than 60 % of a full build. Every publish is recorded (sync_publish.json; build_serving_db records too). The full build stays the weekly refresh's step. Test: tests/ops/test_sync_publish.py builds both on a synthetic lake; every table is equal (EXCEPT ALL both ways, exact for non-float columns, floats to 10 significant digits, because DuckDB's parallel sums differ in the last bits even between two full builds).
- [x] **Triggers** (trigger.py). The supervisor starts the sync about 90 s after the API is ready, every 30 min after the previous one ends, and on request (tray "Sync now", POST /system/sync, `python -m app.ops.sync request`). It runs as a one-shot child in its own Job Object at BelowNormal priority, never two at once, and never while a refresh DAG holds its lock. A refresh that wants to start writes sync.YIELD: the sync starts nothing new and exits (runner DAG_WAITS waits on `sync`, max 3 h). The gap filler yields during pass 1. Settings: config/ops.yaml `sync`, env BIG360_SYNC=0, supervisor --no-sync.
- [x] **UI.** Header chip (`Syncing data... 12/48` -> `Data up to date as of ...` or `N skipped`, linking to Data Health #sync; tooltip with checked / due / fetched / skipped per reason). Data Health "Data sync" panel: dataset rows with status, reason, data as of, expected, next update or retry, jobs run with durations, entities, jobs tab, publishes, notes, Sync now. Tray: "Sync now" item, status line, and a toast when a sync finishes with failures (or "Data up to date as of ..." after a startup / manual sync). API 1.41.0: GET /system/sync, POST /system/sync.
- [x] **Tests.** tests/ops/test_sync_scan.py (fresh, due with params, ledger -> not_yet_published / source_unavailable, needs_key with key names only, no_source, failed check, macro due / lagging / next, CFTC Friday, metered, calendar helpers); test_sync_run.py (every reason path with a fake runner: not published, key missing, source down, failed + parked with alert, back-off growth, partial run, network parallel / heavy sequential, no overlap, yield, interrupted, memory pressure, file-race retry); test_sync_trigger.py (startup after API ready, interval, requests, no overlap, disabled, stop, tray Sync now + toast, supervisor); test_sync_publish.py (section map, incremental == full, as_of and code dirtiness); test_api_serving.py::test_system_sync; frontend src/api/ops/__tests__/sync.test.ts.
- [x] **Real runs.**
  - Run 1 (2026-10-27 23:24 -> 10-28 01:47 UTC, data 2026-10-23 -> 2026-10-27): 39 datasets due; pass 1 in 23 min + a full build in 8 min. Pass 2 analytics were cut short by a false "refresh DAG weekly started": the filler probes the DAG lock files by acquiring them for an instant. The lock check is now confirmed 3 times and rate-limited.
  - Run 2 (2026-10-29 23:25 -> 10-30 04:25 UTC, 5 h 0 min, data 2026-10-27 -> 2026-10-29): 49 datasets due; 114 jobs ran, 18 derive jobs were skipped as unchanged. Pass 1 took 31 min including the full publish (13 min). Pass 2 took 4 h 29 min, including the publish (11 min).
  - Run 3 (2026-10-30 04:27 -> 04:45 UTC, interval-style, pass 1 only, 17.6 min): only the remaining deltas ran (Polygon 10-29, Yahoo for 1,638 symbols, filings, Form 4, finalise of EU / LATAM / US, 16 core derive jobs). 10 of 23 sections were dirty, at 96 % of the full-build cost, so it ran a full publish (8.9 min).
  - Section-incremental publish on the live snapshot (job serving_sections_publish, sections cftc + macro_v3): 69 s including a 30 s copy of the 7 GB file. The API hot-swapped to serving_SI20261030T0447021A3K, and the rebuilt tables are row-identical to the previous full build.
  - New data_as_of: 2026-10-29 for US, UK, JP, KR, HK, CN, CA, APAC and LATAM. EU stays at 2026-10-28 because Yahoo serves NaN for the EU 10-29 bar (checked directly at 04:30 UTC). It is `not_yet_published`, retried with back-off.
  - Still skipped, with their reason: EU 10-29 (`not_yet_published`); FINRA settlement 10-15 (`not_yet_published`, attempt 2); OPENDART_APIKEY (`needs_key`, plus KRX, BANXICO, INEGI, BCCH, ESTAT, BOK keys on secondary sources); TWD, BGN, DE/IT/DK/FI shorts (`no_source`).
  - Failed jobs in run 2: fundamentals_normalize (60-min timeout; the next sync gives it 88 min) and sec_xbrl_facts (file race with cn_fundamentals on fundamentals_raw; now re-queued alone). Both retry on the next sync.
  - Peak RAM of the sync tree: 15.8 GB working set during run 2 (01:01 UTC, fundamentals_normalize thread job + GDELT); 36 GB private commit at 23:33 (pass 1 fan-out); lowest machine free RAM 0.13 GB at that moment. Run 3: 14.4 GB working set.
- [ ] Go-live: `cd frontend && npm run build:app`, then one app restart (tray Quit, start BiG360.exe). That starts the supervisor trigger (sync about 90 s after the API is ready, then every 30 min), the API 1.41+ endpoints, the tray "Sync now" item and toast, the runner's `sync` DAG wait, and the filler yield.
