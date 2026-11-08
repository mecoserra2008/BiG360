# Roadmap v2: items 6, 5, 4, 7, 8, 9 (resumable plan)

**User decisions (2026-10-19):** execute in this order: (6) earnings & corporate-events calendar, (5) cross-asset dashboard, (4) backtesting lab, (7) smart-money page, (8) thesis tracker, (9) supply-chain & revenue-geography map. Items 1, 2, 3 and 10 are parked. **Start only after the user says "go".**

**Standing rules:**
- Macro inputs come from official sources only; anything model-based is labelled.
- Point-in-time correctness: knowledge_date / release time on every fact.
- Contracts are additive only: bump api.yaml and log in `docs/CONTRACT_CHANGES.md`.
- Resumable state in `data/state/roadmap_v2.json`; long jobs run detached.
- No raw-data bloat: compact storage.
- Each item ships backend, then UI, then verification (headless Chrome, both themes, zero console errors), with build/lint/tests clean.
- The user's API (:8010) is restarted only at the end of each item; the GDELT backfill is never stopped.

---

## 6. Earnings & corporate-events calendar
**Goal:** one place that answers "what is reported or happening, when, and how does this name usually react?" Includes model-based expectations, since no consensus feed exists.

**Data:**
- **Historical earnings release times (official, point-in-time):**
  - US: SEC 8-K Item 2.02 acceptance timestamps (already in filings_index), plus 10-Q/10-K filing dates.
  - JP: TDnet kessan tanshin.
  - KR: DART.
  - HK: HKEX results announcements.
  - EU: ESEF/national archive deposit times.
  - BR: CVM.
  - UK: FCA NSM.
- **Upcoming dates:**
  - company-announced dates where filed (8-K/press RSS, TDnet schedules, HKEX board-meeting notices);
  - otherwise Yahoo/Polygon earnings dates (company data, labelled "provider estimate");
  - otherwise our own **projected date** from each company's reporting cadence (labelled model-based).
- **Other corporate events:**
  - dividends: ex-date, pay date, amount (corporate_actions);
  - splits;
  - from 8-K items: M&A 1.01/2.01, executive changes 5.02, buyback/financing 1.01/8.01;
  - AGMs where filed; index-relevant events (spin-offs from corporate actions).

**Analytics:**
- Earnings reaction history per company: event CAR/BHAR (reuse event_car) over [−1,+1], [0,+5] and [+1,+20], plus the implied move vs the realised-volatility baseline, and gap fill / drift statistics.
- **Model-based expectations** for revenue/EPS: seasonal ARIMA on the company's own quarterly series, plus the analytics-forecast path, with an interval. **Surprise** = actual vs model when the filing lands. Labelled "model-based (not consensus)".
- Sector earnings-season tracker: share reported, aggregate model-surprise, reaction breadth.
- Post-earnings drift signal (PEAD on model surprise), for the backtesting lab (#4).

**API/UI:**
- Calendar page (day/week/month; filters by universe, watchlist, region, sector, event type).
- Company 360 "Events & Earnings" panel (next date with source label, past reactions chart, model expectation vs actual history).
- Earnings-season dashboard.
- Alerts: upcoming earnings for watchlist names, big model surprise.
- Live feed integration: 8-K 2.02 lands → surprise computed.

**Risks:**
- Upcoming dates outside the US/JP rely on provider or projected dates, labelled as such.
- EPS from XBRL needs care: diluted vs basic, quarterly derivation (Q4 = FY − 9M).

## 5. Cross-asset dashboard
**Goal:** one view of positioning, carry, curve, volatility and trend per asset class: rates, FX, commodities, equity indices, credit, crypto futures.

**Data (all already present except the curves):**
- prices for futures, FX, indices and ETFs;
- CFTC positioning (agent running);
- official rates and policy paths (W3);
- official FX (ECB/FRED);
- volatility indices;
- OAS (FRED);
- **new: futures term structures**, from Yahoo specific contract-month tickers (e.g. CLZ26, CLF27…). Market data, labelled "Yahoo"; months auto-rolled. Backfill a few years of daily curves; nightly incremental.

**Analytics per asset:**
- trend/momentum (1/3/12m, trend-following signal);
- realised and implied vol and their ratio;
- carry:
  - FX: official short-rate differentials (policy or 3m rates);
  - futures: roll yield from the curve;
  - bonds: yield minus funding, plus roll-down;
  - equities: dividend yield minus funding;
- value proxies (real-rate-adjusted FX, curve shape);
- CFTC COT index and flows;
- cross-asset correlation regime (rolling correlation matrix, eigen-concentration = "everything moving together" risk);
- a risk-on/risk-off composite (model-based);
- each asset's sensitivity to the macro regime (W4 regime_performance).

**API/UI:**
- **Cross-asset cockpit:** heatmap of assets × signals (trend, carry, vol, positioning, value) with z-scores; curve viewer (current vs 1m/1y-ago term structure, contango/backwardation history); correlation-matrix regime chart; risk-on/off gauge with history.
- Instrument pages gain curve and carry panels.
- "Open in Studio / Backtest" for any signal.

**Risks:** Yahoo contract-month coverage has gaps (handle missing months); the carry definitions are documented per asset class.

## 4. Backtesting lab
**Goal:** test whether signals and screens actually make money, point-in-time, with honest statistics.

**Engine (`app/backtest/`):**
- **Universe:** any universe/screen rule evaluated point-in-time each rebalance (scores, ratios, factors as known then). Survivorship caveat banner; delisted US names are included where priced.
- **Signals:** factor scores, composites, ML predictions, technical score, insider/sentiment/event signals, CFTC/carry/trend (cross-asset), macro regimes, user formulas (formula engine), and Studio model outputs.
- **Portfolio construction:** rank/quantile long-only or long-short; equal / cap / signal / inverse-vol / risk-parity weights; sector/country neutrality; position caps; turnover limits; rebalance daily/weekly/monthly; holding-period overlap handling.
- **Costs:** commissions plus spread/impact model (bps by liquidity bucket from ADV); borrow cost for shorts; FX conversion with official rates.
- **Event-driven mode:** enter on event (insider cluster, model earnings surprise, CFTC extreme, macro surprise, confluence-zone touch); exit on time/stop/target.
- **Outputs:**
  - equity curve vs benchmark;
  - CAGR, vol, Sharpe/Sortino, max drawdown, hit rate, turnover, exposure;
  - factor attribution (our factors + market/size/value/momentum proxies);
  - decile spreads; regime breakdown; rolling metrics.
- **Overfitting controls:**
  - parameter sweeps in walk-forward;
  - **deflated Sharpe ratio**, **probability of backtest overfitting (CSCV)**;
  - a multiple-testing counter per study;
  - a mandatory out-of-sample period.
- **Reproducibility:** spec hash, snapshot id, export to Python (like Model Studio).

**API/UI:**
- Backtest builder (universe → signal → construction → costs → period), with run async on the worker.
- Results tabs (performance, risk, attribution, trades/holdings, robustness).
- Compare runs; "Backtest this screen" from the Screener; "Backtest this signal" from Studio, cross-asset and CFTC.
- Saved strategies, schedulable as paper portfolios, re-run weekly with drift alerts (reusing Studio scheduling).

**Risks:** compute (vectorised Polars/DuckDB; cache panels); point-in-time correctness is central (reuse the pit helpers and leakage tests: label permutation must give ≈0).

## 7. Smart-money page
**Goal:** who is buying, selling, shorting and crowding, per company and sector.

**Data:**
- SEC 13F (quarterly; knowledge_date = filing date, not quarter end);
- FINRA short interest (twice monthly) + fails-to-deliver;
- insider transactions/signals (US + KR/UK/DE/NL/HK/BR);
- **new: EU/UK official net short-position disclosures** (≥0.5% holders, published by FCA, AMF, CNMV, BaFin, CONSOB, AFM; verify the machine-readable lists), giving named short sellers for European stocks;
- CFTC for futures (from #5);
- 13D/13G activist and large-holder filings (already in the EDGAR index).

**Analytics:**
- institutional ownership % and change, holder count, new/added/trimmed/exited positions;
- concentration (HHI), crowding (top-holder overlap across "hedge-fund-like" filers);
- "smart cohort" behaviour (filers selected by past performance, computed point-in-time, model-based);
- short interest % float, days to cover, SI z-score and change;
- squeeze-risk flag (high SI + rising price + low float);
- activist 13D events;
- a combined **smart-money score** (model-based) feeding the scoring Insider/Sentiment families optionally, and the backtesting lab.

**API/UI:**
- Smart-money page: top accumulation/distribution, most shorted, squeeze watch, activist feed, EU short-seller register.
- Company 360 "Ownership" tab: holders table, ownership trend, short interest trend, insider timeline.
- Sector aggregates. Alerts: new 13D, SI spike, new EU short position.

**Risks:** the 45-day 13F lag must be visible in the UI; EU disclosure formats vary by regulator.

## 8. Thesis tracker
**Goal:** turn analysis into a decision log with accountability.

**Data model (app_state):**
- `theses`: security/sector/macro theme, direction, horizon, conviction, thesis text, key drivers, catalysts with dates, KPIs to watch with thresholds, entry/target/stop bands, invalidation conditions, linked scenario (W4) and linked backtest/Studio run.
- `thesis_journal`: dated notes and attachments.
- `thesis_reviews`.

**Automation:**
- auto-create alert rules from the thesis (price bands, KPI thresholds via the formula engine, score/grade change, earnings date from #6, event shocks, CFTC/smart-money triggers);
- review reminders;
- automatic status: "on track / at risk / invalidated", from rules plus the latest data;
- performance since inception vs benchmark and sector, hit rates across theses;
- a post-mortem template on close;
- export (Markdown/PDF).

**UI:**
- Thesis board (kanban: idea → active → closed) with filters.
- Thesis page: timeline of journal entries and triggered alerts on a price chart, KPI tracker, scenario impact, linked evidence (filings, stories, events).
- "Create thesis" from Company 360, Screener, Studio and Backtest.

**Risks:** single-user local, so no auth needed; storage stays small.

## 9. Supply-chain & revenue-geography map
**Goal:** see how a company is exposed through its customers, suppliers and countries, and what is happening there.

**Data:**
- linkage `company_links` (customers 6.1k; co-mentions);
- `company_geo_revenue` (2k issuers; mostly US);
- event models (country intensity, escalation, hotspots);
- macro (cycle, surprises, recession risk per country);
- scenario engine.

**Improvements:**
- better supplier/customer extraction from 10-K/20-F text (rules on "customer", "supplier", "% of revenue" patterns; no LLM);
- reverse edges (supplier lists of disclosed customers);
- return-correlation edges as a fallback "economic neighbours" layer (labelled statistical);
- ESEF/annual-report geographic segments where tagged.

**Analytics:**
- second-order exposure (my customers' countries/sectors);
- network centrality and concentration risk (top-customer share);
- exposure-weighted country risk (events + macro + recession);
- contagion paths: a shock in country/company X → impact estimates via the network + W4 betas (model-based).

**UI:** Company 360 "Network & Geography" tab:
- force-directed supply-chain graph (nodes coloured by score/events, edges by revenue share);
- revenue-geography choropleth with event/macro overlays;
- an exposure table;
- "shock this node" scenario button.

Also a portfolio/watchlist-level map (aggregated exposure).

**Risks:** sparse data outside the US, shown honestly with coverage badges.

---

## Execution order & estimates (agent wall time, approximate)
1. **#6:** backend ~1 session, UI ~1 session.
2. **#5:** curve ingestion + signals + UI (needs CFTC, which is being built now).
3. **#4:** the largest item: engine + overfitting stats + UI, ~2–3 sessions.
4. **#7:** 13F/SI analytics + EU short registers + UI.
5. **#8:** app_state + automation + UI.
6. **#9:** extraction improvements + network analytics + UI.

Independent backend parts may overlap with the previous item's UI work.

## Item 6 sub-steps (state: `data/state/roadmap_v2.json` key `item6`)
- [x] 6.1 Explore data assets & conventions
- [x] 6.2 Contracts: schemas earnings_events, earnings_calendar, corporate_events, earnings_reactions, earnings_expectations, earnings_season, earnings_pead
- [x] 6.3 `earnings_events` job (SEC 8-K 2.02 from submissions + periodic filings across regulators + TDnet tanshin)
- [x] 6.4 `earnings_calendar` job (company-announced > provider estimate (Yahoo) > model-based projection from cadence)
- [x] 6.5 `corporate_events` (dividends/splits/spin-offs + 8-K items 1.01/2.01/5.02/8.01 + AGMs)
- [x] 6.6 `earnings_reactions` (CAR/BHAR [-1,+1],[0,+5],[+1,+20]; realised-vol baseline; gap fill/drift)
- [x] 6.7 `earnings_expectations` (SARIMA on own quarterly rev/EPS, Q4 = FY - 9M, diluted EPS; intervals; surprises)
- [x] 6.8 `earnings_season` sector aggregates + `earnings_pead` signal
- [x] 6.9 Jobs in contracts/jobs.yaml nightly DAG
- [ ] 6.10 History backfill run (logs in data/logs/roadmap_v2/) — phase 1 done (SEC submissions all 6,295 CIKs + Yahoo priority universes + all analytics); phase 2 (Yahoo dates for the rest of `full`, then analytics again) running detached: `scripts/earnings_backfill_chain.py --full-only` (resumable; re-run the same command if interrupted)
- [x] 6.11 Serving tables + API endpoints (calendar, company earnings, season) + api.yaml bump
- [x] 6.12 Alert rule types (upcoming earnings for watchlist, big model surprise) + live bus publish on 8-K 2.02
- [x] 6.13 Frontend: Calendar page, Company 360 Earnings & Events panel, Earnings-season dashboard, nav
- [x] 6.14 Tests (pytest, vitest), ruff, tsc, vite build, headless Chrome both themes

## Item 5a sub-steps: curve data + signals (state: `data/state/roadmap_v2.json` key `item5a`; definitions `docs/CROSS_ASSET_DEFINITIONS.md`)
- [x] 5a.1 Probe the Yahoo contract-month format (`{ROOT}{M}{yy}.{NYM|CMX|CBT|CME|NYB}`; live contracts only, expired not served) and listed months per root
- [x] 5a.2 Contracts: futures_curve, futures_contracts, cross_asset_signals, cross_asset_regime, cross_asset_corr
- [x] 5a.3 `app.ingestion.futures_curve`: 43 roots, exchange listing cycles, approximate expiry rules, paced fetch with throttle handling, missing months recorded
- [x] 5a.4 Detached backfill (scripts/roadmap_v2_item5a_launch.ps1, logs data/logs/roadmap_v2/item5a_backfill.*): 257 contracts, 250 served / 7 missing, 205k rows, 2.9 MB
- [x] 5a.5 Jobs: futures_curve (nightly), futures_curve_backfill (manual), cross_asset_signals (nightly) in contracts/jobs.yaml
- [x] 5a.6 `app.analytics.cross_asset`: 73 assets; trend/momentum, rv/iv, carry by class, curve roll yield/slope/shape, value proxies, z-scores; nullable cot_* placeholders
- [x] 5a.7 Correlation regime (60/120d eigen-concentration, fixed 12-asset basket, matrices) + model-based risk-on/off composite (8 components)
- [x] 5a.8 Tests (tests/ingestion/futures, tests/analytics/test_cross_asset.py), ruff clean; jobs run once via `app.cli jobs run`
- [x] 5b CFTC join into cot_* columns, API (api.yaml), serving, cockpit UI (sub-steps below)

## Item 5b sub-steps: API, serving + UI (state: `data/state/roadmap_v2.json` key `item5b`)
- [x] 5b.1 Explore plan, definitions, 5a modules, positioning router / page pattern
- [x] 5b.2 CFTC join in the `cross_asset_signals` job (`join_positioning`: root = cftc_markets.root, disagg managed money / TFF leveraged funds, latest report released <= date, stale > 28 d -> null); soft dep on cftc_signals; job rerun
- [x] 5b.3 Serving tables (xa_*) in build_serving_db
- [x] 5b.4 Router /cross-asset/* + api.yaml (additive) + CONTRACT_CHANGES + Studio source `cross_asset`
- [x] 5b.5 Frontend: Cross-asset page (heatmap, curve viewer, correlation regime, risk gauge), Instrument curve / carry panels, nav
- [x] 5b.6 Tests (pytest join PIT + API + fixture; vitest adapters), ruff, tsc, lint, vite build, headless Chrome both themes
- [x] 5b.7 build_serving_db + restart the user API on :8010

## Item 4 sub-steps: backtesting lab (state: `data/state/roadmap_v2.json` key `item4`)
Code `backend/app/backtest/`; runs `data/backtest/runs/<run_id>/`; panel cache `data/cache/backtest/` (bounded).
- [x] 4.1 Explore data assets (factor_scores 10y monthly PIT for 781 mvp names, composites, ML OOS walk-forward, earnings_pead, cross_asset_signals + cot_*, macro_factors states), Studio engine / store / scheduling, ML leakage tests
- [x] 4.2 Engine core `_engine.py` (standalone): rank/quantile long-only / long-short, equal / cap / signal / inverse-vol / risk-parity, sector / country neutral, caps, turnover limit, D/W/M rebalance, overlapping holding periods; daily simulation; costs (commission, spread + sqrt impact by ADV bucket, borrow, FX)
- [x] 4.3 PIT panel builder `_panel.py` + signal catalog (all sources) + PIT universe / screen rules + delisted names + disk cache
- [x] 4.4 Event-driven mode (insider cluster, earnings model surprise, CFTC extreme, macro surprise, confluence touch, signal threshold; exits time / stop / target)
- [x] 4.5 Analytics: benchmark, metrics, rolling, factor attribution, decile spreads / IC, regime breakdown (W4 states)
- [x] 4.6 Overfitting controls: walk-forward sweeps, deflated Sharpe, PBO (CSCV), multiple-testing counter, mandatory OOS holdout, permutation + stale-signal diagnostics
- [x] 4.7 Reproducibility: spec hash + snapshot id, Python export (embedded cores)
- [x] 4.8 Correctness tests (look-ahead, permutation, toy portfolios, costs, DSR / PBO known values) + performance on the real lake -> tests/backtest (24 engine + 6 panel incl. export reproduction); 10y monthly mvp_us value L/S with 20 permutations + 6-config sweep 27.6 s (panel 13.6 s uncached)
- [x] 4.9 Persistence: app_state `bt_strategies`, `bt_runs` (contracts/schemas)
- [x] 4.10 Jobs: `backtest_run` (async, file queue), `backtest_scheduled` (weekly paper re-runs + drift / decay alerts)
- [x] 4.11 Router `/backtest/*` + api.yaml (additive, 1.24.0; smart-money then took 1.25.0) + CONTRACT_CHANGES + API tests
- [x] 4.12 Frontend: Backtest Lab (builder, results tabs, compare, saved strategies), adapters, schema.d.ts
- [x] 4.13 Entry points: Screener "Backtest this screen"; Studio / Cross-asset / CFTC "Backtest this signal"
- [x] 4.14 Verification: vitest, tsc, vite build, oxlint, headless Chrome both themes (8030 / 5182) -> 0 console errors at 1440 / 1024; remaining: restart API :8010 + worker (no serving rebuild needed)

## Item 7 sub-steps: smart-money page (state: `data/state/roadmap_v2.json` key `item7`; scratch `scratchpad/x7/`; logs `data/logs/roadmap_v2/x7_*`)
- [x] 7.1 Explore data assets (13F one quarter only = 2026Q2 2.5M rows + stray periods; FINRA SI one settlement; FTD one half-month; 13D/G in filings_index without filer CIK) + verify EU/UK short registers live (formats in `docs/SMART_MONEY.md`)
- [x] 7.2 History backfills (detached, resumable): FINRA SI + FTD since 2023 (existing job, params since / ftd_n_latest); 13F datasets 2023q2 .. 01mar2026-31may2026 (`app.ingestion.sec_13f_backfill`, DuckDB staging, zips deleted after staging); 13D/G from EDGAR full-index 2023Q1+
- [x] 7.3 EU/UK net short positions ingestion `app.ingestion.eu_short` -> `eu_short_positions` (FCA, AMF, CNMV, AFM, CBI, FSMA, FI-SE, Finanstilsynet-NO; CONSOB / BaFin / DK / FI skipped with reasons)
- [x] 7.4 13D/13G filer + subject feed `app.ingestion.sec_13dg` -> `activist_filings`
- [x] 7.5 13F analytics `app.analytics.smart_money_13f` -> `inst_ownership`, `inst_filers` (HF-like classification, model-based smart cohort)
- [x] 7.6 SI / FTD / activism / combined score `app.analytics.smart_money` -> `si_signals`, `activist_events`, `smart_money_signals` (+ rank-IC sanity check)
- [x] 7.7 Contracts (schemas) + jobs.yaml + optional scoring input (flag, default OFF)
- [x] 7.8 Serving tables + router /smart-money/* + /companies/{id}/ownership + api.yaml (additive) + alerts + Studio source
- [x] 7.9 Frontend: Smart-money page (overview, EU register, sectors), Company 360 Ownership tab, adapters, schema.d.ts
- [x] 7.10 Tests (pytest parsers / PIT / math / API; vitest), ruff, tsc, oxlint, vite build, headless Chrome both themes (8050 / 5190)

## Item 8 sub-steps: thesis tracker (state: `data/state/roadmap_v2.json` key `item8`; scratch `scratchpad/x8/`)
- [x] 8.1 Explore app_state layer (studies / watchlists / alerts / peer_sets / macro_scenarios / backtest), alerts engine, formula engine, W4 scenarios, Studio, frontend pages
- [x] 8.2 Contracts: app_state schemas theses, thesis_journal, thesis_reviews, thesis_snapshots; alerts v6 (nullable thesis_id + thesis_* rule types)
- [x] 8.3 Store `app.theses.store` (CRUD, journal, reviews, snapshots; migrations via ensure_duckdb_tables)
- [x] 8.4 Linked alert rules generated on create / update (price bands, KPI thresholds, score / grade, earnings, event shocks, CFTC / smart-money), tagged thesis_id
- [x] 8.5 Evaluator `app.theses.evaluate` (KPIs via the formula engine, automatic status on track / at risk / invalidated with reasons, review reminders, catalysts, positioning / event shocks) + nightly job `thesis_eval` (jobs.yaml)
- [x] 8.6 Performance since inception vs benchmark and sector (shared price reader, FX), hit-rate stats, post-mortem template, Markdown export (+ print CSS)
- [x] 8.7 Router /theses/* + api.yaml (additive) + CONTRACT_CHANGES + API tests
- [x] 8.8 Frontend: thesis board (kanban), thesis page, editor, "Create thesis" entry points (Company 360, Screener, Studio, Cross-asset, Backtest Lab), schema.d.ts
- [x] 8.9 Tests (pytest, vitest), ruff, tsc, oxlint, vite build
- [x] 8.10 Headless Chrome both themes (test API :8060 + Vite :5195 on an isolated data copy)

## Item 9 sub-steps: supply-chain & revenue-geography map (state: `data/state/roadmap_v2.json` key `item9`; scratch `scratchpad/x9/`; definitions `docs/SUPPLY_CHAIN.md`)
- [x] 9.1 Explore linkage (company_links, company_geo_revenue, get_W), event models, macro (cycle / surprises / recession), W4 scenarios, sec_text corpus, ESEF raw packages, Company 360 / Global Events map
- [x] 9.2 Contracts: company_links (additive: supplier edges, provenance snippet / rule / form), company_geo_revenue (additive: method, confidence), new company_neighbours, sc_exposure, sc_metrics, country_risk
- [x] 9.3 Link extraction v2 (rules only): customer / supplier / "% of revenue" / "accounted for X% of net sales" patterns on 10-K / 20-F / 40-F text (bounded, cached per filing), entity matching with confidence, supplier (reverse) edges, provenance
- [x] 9.4 Statistical economic neighbours (return correlation + partial correlation, labelled statistical) -> company_neighbours
- [x] 9.5 ESEF / annual-report geographic segments: GeographicalAreasAxis facts (all revenue concepts) + segment / geographic-area text-block tables of the raw ESEF packages -> company_geo_revenue
- [x] 9.6 Analytics `app.supply_chain`: second-order exposure (customers' countries / sectors), centrality, concentration (top-customer share, HHI), exposure-weighted country risk (events + escalation + recession + surprises), damped contagion propagation (model-based), watchlist aggregation
- [x] 9.7 Jobs in contracts/jobs.yaml (soft_depends_on)
- [x] 9.8 Serving tables + router (company network / geography, shock simulation, watchlist exposure) + api.yaml (additive) + CONTRACT_CHANGES + fixture + API tests
- [x] 9.9 Frontend: Company 360 "Network & Geography" tab (graph, choropleth, exposure table, shock panel, coverage badges), Watchlist exposure map, adapters, schema.d.ts
- [x] 9.10 Tests (pytest extraction / propagation / PIT / API; vitest), ruff, tsc, oxlint, vite build, headless Chrome both themes (8070 / 5197)

## Progress
- [ ] 6 Earnings & corporate-events calendar
- [x] 5 Cross-asset dashboard
- [ ] 4 Backtesting lab
- [x] 7 Smart-money page (backend + UI done; needs the coordinated build_serving_db + API restart)
- [x] 8 Thesis tracker (backend + UI done; needs an API + worker restart, no serving rebuild)
- [x] 9 Supply-chain & revenue-geography map (backend + UI done; needs the coordinated build_serving_db + API restart)
