# Macro v2: Waves 1 and 2 (resumable plan)

**User decisions (2026-10-12):**
- Build Waves 1 and 2 now.
- "Expected" values come from our own model forecasts, clearly labelled **model-based** everywhere they appear. No consensus feed.
- Approximations are acceptable, but **inputs must come from official sources**: central banks, statistical offices, treasuries, regulators, the NY Fed and other Fed banks, OECD, BIS, IMF, Eurostat. FRED counts as official (St. Louis Fed). No Yahoo or scraped commercial sites for macro.

**Resume protocol:**
- Tick items `[x]` here with a one-line result.
- State files: `data/state/macro_v2_w1.json` and `data/state/macro_v2_w2.json`.
- Long backfills run detached and are resumable.
- Contract changes are additive only: bump `api.yaml` and log them in `docs/CONTRACT_CHANGES.md`. Re-read shared files immediately before editing; two agents work in parallel.

## Wave 1: macro cockpit (owner: macro-w1)
- [x] W1.1 **Indicator standardization layer** (`app/macro/indicators.py`). Map catalog series to canonical (country, indicator) cells: GDP q/q and y/y, IP, retail, PMI proxy (OECD BCI/CLI, EC ESI), CPI headline and core, PPI, unemployment, employment, policy rate, 2Y, 10Y, 2s10s, credit growth, M2/M3, house prices, current account % GDP, fiscal balance and debt % GDP, REER. _Done 2026-10-12: 430/552 cells (24 countries x 23 indicators incl. derived 2s10s + EA inheritance for euro members), tables macro_cells (PIT snapshots, 36 month-end backfill) + macro_cell_history; job macro_cells ~6 s nightly._
  - Per cell: latest value, period, release date; z-score vs the country's own 10y history; 3- and 6-month change (direction); percentile; "as first released" vs "latest revised" where vintages exist; source and series_id.
  - A table `macro_cells` (point-in-time, daily snapshot) plus history.
- [x] W1.2 **Cycle clock.** Per country, growth momentum (a composite of z-scored GDP/IP/retail/PMI-proxy changes) × inflation momentum (CPI/core/PPI changes), giving quadrants recovery / expansion / slowdown / contraction, with a 12-month trail. Write a `macro_cycle` table. Method documented and reproducible. _Done: app/macro/cycle.py (cycle_v1, method in docstring + API), macro_cycle 8,184 rows; 23/24 countries placed (TW lacks growth inputs); job macro_cycle ~3 s._
- [x] W1.3 **Country pages data** for 24 countries (US, EA, DE, FR, IT, ES, PT, NL, GB, CH, JP, KR, CN, HK, TW, IN, AU, CA, BR, MX, CL, CO, PE, AR): sections growth, inflation, labor, rates and curve, credit, housing, external, fiscal, FX, and the equity index vs a macro composite. Each section lists its series with sparkline-ready history. _Done: app/macro/countries.py; 9 sections + equity index vs cycle composite (CL/CO/PE: no official-index price series in the lake)._
- [x] W1.4 **Thematic dashboards**, data plus new official series where they're missing: _Done: app/macro/themes.py (7 themes, 40 panels); 35 new official series ingested (FRED CPI components/DFII5/WTREGEN/SLOOS/JOLTS rates, ECB HICP comps/ILM/BLS, ONS CPI comps, NY Fed ACM via new nyfed fetcher)._
  - **Inflation:** components where official breakdowns exist (US CPI components via FRED/BLS, EA HICP components via Eurostat/ECB, UK via ONS); breakevens and real yields (FRED: T5YIE, T10YIE, DFII10); PPI.
  - **Labor:** payrolls, claims, JOLTS (FRED); unemployment and employment across regions.
  - **Rates and curves:** curve history for animation; 2s10s and 3m10y spreads; real yields; **ACM term premium (NY Fed, official CSV)**; EA AAA/all curves (ECB).
  - **Credit and financial conditions:** OAS (FRED ICE BofA archive); NFCI/ANFCI (Chicago Fed); STLFSI; ECB CISS; Fed SLOOS and ECB BLS lending surveys.
  - **Liquidity:** Fed balance sheet WALCL, reverse repo RRPONTSYD, Treasury General Account WTREGEN (official, via FRED); ECB balance sheet; M2 by region.
  - **Commodities and FX:** World Bank Pink Sheet, FRED, ECB/Fed FX.
- [x] W1.5 **Revisions toggle.** For series with ALFRED/vintage data: a "first release" vs "latest" series pair, and a revision-size stat. _Done: app/macro/revisions.py -> macro_revisions (ALFRED full + first-seen revised periods), PIT first-release canonical values, revision stats 5y/10y/all in /series._
- [x] W1.6 **API endpoints:** `/macro/v2/heatmap`, `/macro/v2/cycle`, `/macro/v2/countries`, `/macro/v2/countries/{cc}`, `/macro/v2/themes/{theme}`, `/macro/v2/series/{id}` (with vintages, transforms and provenance), `/macro/v2/compare` (multi-country, same indicator). Serving snapshot tables. Tests. _Done: routers/macro_v2.py, api.yaml 1.8.0 (7 ops, 23 MacroV2* schemas), serving tables macro_cells/macro_cell_history/macro_cycle/macro_revisions/macro_vintages, tests/macro_v2 + test_api_serving.test_macro_v2_wave1._

## Wave 2: what's new and what's surprising (owner: macro-w2)
- [x] W2.1 (macro-w2 2026-10-12: FRED release dates + 19 official calendars live, 6,307 releases / 5,918 events, 294 official upcoming in 90d; app/macro/calendar.py + calendar_sources.py) **Official release calendar** (`app/macro/calendar.py`). The FRED releases/dates API (US releases incl. BLS/BEA/Census schedules), BLS release schedule, BEA schedule, Eurostat release calendar, ECB statistical calendar and meeting dates, ONS release calendar API, BoE MPC dates, BoJ schedule, Statistics Canada and Bank of Canada schedules, ABS/RBA, IBGE/BCB Copom, INE/INSEE/Destatis/ISTAT where available.
  - Tables `macro_releases` (scheduled datetime with timezone, release, country, linked series_ids, importance tier) and `macro_release_events` (actual, timestamp first seen, prior, revision of prior).
  - Past + 90 days ahead.
- [x] W2.2 (9,683 model-based expectations, 171 series; PIT backfill 9,595 rows; SARIMA beats naive on 120/168 series) **Model-based expectations.** Before each scheduled release, a nightly job snapshots a forecast of the upcoming value for each linked core series: ARIMA/SARIMAX with order search, a seasonal-naive fallback, nowcast-informed where available. Store `macro_expectations` with forecast_made_at < release time (point-in-time, immutable), the model, the interval and a version. **Label "Model-based expectation (not consensus)".**
  - Backfill: generate historical expectations point-in-time using vintages (ALFRED) or the data as of each release date, so surprise history exists from day one.
- [x] W2.3 (4,794 surprises, 3,768 with z; daily index for 24 countries + GLOBAL x 4 themes) **Surprise indices.** Surprise = (actual − model expectation) / historical forecast-error std. Exponentially-weighted surprise index per country and per theme (growth / inflation / labor), Citi-style. Table `macro_surprise_index` with daily history.
- [x] W2.4 (DynamicFactorMQ US/EA/GB/JP, weekly PIT history since 2023-07 with news; GDPNow + NY Fed Staff Nowcast benchmarks) **Nowcasts.** US and EA real GDP (and optionally UK, JP) with statsmodels `DynamicFactorMQ` (mixed monthly/quarterly), re-estimated as each release lands. Store a nowcast history (with a news decomposition: which release moved it) in `macro_nowcasts`.
  - Show official benchmarks alongside as reference: Atlanta Fed GDPNow and NY Fed Staff Nowcast (official CSV/Excel) where downloadable.
- [x] W2.5 (macro_recession: NY Fed official, our probit for 9 countries, Sahm, HMM, OECD CLI turning points; 10,706 rows) **Recession-risk panel:**
  - NY Fed yield-curve recession probability (official, published; plus our own re-estimate of the same probit for other countries using their 3m10y spreads)
  - Sahm rule real-time (official FRED SAHMREALTIME)
  - our HMM regime probabilities
  - OECD CLI turning points
  - all side by side, each with its method label
- [x] W2.6 (macro_release / macro_surprise / regime_change evaluators; alerts contract + router extended) **Alerts:** new rule types `macro_release` (a series updates), `macro_surprise` (|z| > threshold), `regime_change` (cycle quadrant change / recession probability crosses a level). Evaluators in `app/scheduler/alerts.py`; additive alerts contract.
- [x] W2.7 (api 1.9.0: 5 endpoints in routers/macro_v2_w2.py; serving tables; tests) **API:** `/macro/v2/calendar` (filters: country, importance, date range), `/macro/v2/releases/{id}` (actual vs model expectation, surprise z, history), `/macro/v2/surprise`, `/macro/v2/nowcast/{cc}`, `/macro/v2/recession`. Serving tables. Tests.

## Jobs
Additive in `jobs.yaml`:
- **nightly:** macro_cells, macro_cycle, calendar refresh, expectations, surprise, recession
- **on new data:** nowcast re-estimate
- **weekly:** calendar horizon extend

Everything stays point-in-time.

## Frontend (after the W1.6 and W2.7 contracts land)
- [x] F1 Macro section in the nav: _Done 2026-10-12 (frontend): Macro nav section (Cockpit, Calendar, Nowcast, Themes, Countries, Series explorer, Markets); heatmap (good_direction-oriented z, arrows, tooltips with provenance, series drawer), cycle clock with trails, recession panel (method labels, official vs ours), surprise snapshot, upcoming releases; 24 country pages; 7 theme dashboards incl. animated yield curves + NBER shading; calendar with model-based expectation + interval, surprise z, official/inferred badge, release drawer; nowcast path + news waterfall + official GDPNow/NY Fed + track record; series explorer (transforms, first release vs latest, revision stats, compare, provenance, Open in Model Studio); macro alert rules + events in Watchlist & Alerts. 'Model-based expectation (not consensus)' everywhere; 0 console errors in both themes at 1440/1024; 69 tests, build/lint clean._
  - **Cockpit:** heatmap, cycle clock, recession panel, surprise snapshot, today's and upcoming releases.
  - **Country pages** and **theme dashboards**.
  - **Calendar:** actual vs model expectation, surprise colouring, model-based label.
  - **Nowcast:** fan with news decomposition.
  - **Series explorer:** vintages toggle, compare countries, "open in Model Studio".

  Replace the old single Market & Macro page; keep a market overview.
