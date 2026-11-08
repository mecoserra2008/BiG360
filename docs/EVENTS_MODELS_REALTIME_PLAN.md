# Event modelling (spatial / dynamics / ML) and real-time R1: resumable plan

**User decisions (2026-10-12):**
- Order: linkage data (A), then spatial econometrics (B) and event dynamics (C), then ML (D). R1 runs in parallel.
- Add connection data: UN Comtrade, IMF trade, OECD ICIO/TiVA, BIS banking, Natural Earth, XBRL geographic revenue, 10-K major customers.
- Real-time prices (R2/R3) are deferred. **No Redis/Docker for now**: the bus sits behind an interface with a local SQLite (WAL) implementation, so Redis can be swapped in later.
- Macro rule still applies: macro/linkage inputs come from official or public-domain sources; model-based outputs are labelled.

**Resume protocol:**
- Tick `[x]` with a one-line result.
- State files: `data/state/linkage.json`, `data/state/event_models.json`, `data/state/realtime.json`.
- Long jobs run detached and resumable.
- Contracts are additive only: bump `api.yaml`, log in `docs/CONTRACT_CHANGES.md`, and re-read shared files before editing.

## A. Linkage data & spatial weights (owner: linkage)
- [x] A1 **Geography:** Natural Earth admin-0 (public domain) gives centroids, contiguity (queen/rook) and great-circle distances. Contract `geo_countries`. **Done 2026-10-13:** `geo_countries` 239 countries (NE 5.1.1 10m), libpysal queen/rook (77 islands), label-point distances; `app.linkage.geo`, job `linkage_geo`.
- [x] A2 **Trade:** UN Comtrade bilateral totals (+ HS2 sections where cheap) and IMF Direction of Trade (official SDMX), annual from 2000. Contract `trade_bilateral`. If a Comtrade key is required, skip gracefully, list `COMTRADE_APIKEY` and fall back to IMF. **Done (Comtrade backfill continuing):** IMF IMTS 1.30M rows, 208 reporters x all partners, 2000-2025 (keyless). Comtrade keyless preview works but is quota-limited (~250 calls/h, 1 period/call): detached quota-aware backfill of bilateral totals + HS2-vs-world for 55 reporters (resumable `data/state/linkage_comtrade.json`); `COMTRADE_APIKEY` optional (`.env.example`).
- [x] A3 **Input-output:** OECD ICIO / TiVA inter-country, inter-industry flows (latest editions), mapped to our sector taxonomy. Contract `io_flows`. **Done:** `io_flows` from OECD ICIO 2023 ed. (2016-2020, SML, keyless stats.oecd.org file) mapped to 11 sectors + NONMKT (~0.8M rows/yr). The 2025 edition (to 2022) is only linked from www.oecd.org (403 to scripts): drop its zip into `data/raw/linkage/icio/` and rerun `linkage_io`.
- [x] A4 **Finance:** BIS locational/consolidated banking statistics (bilateral claims). Contract `bank_claims`. **Done:** `bank_claims` 1.10M rows: BIS LBS cross-border claims/liabilities (31 reporters with bilateral detail) + CBS immediate (27) / ultimate-risk (23), 2000Q1-2026Q1.
- [x] A5 **Company → country:** XBRL geographic segment revenue (srt:StatementGeographicalAxis and related members in SEC companyfacts/frames, plus ESEF where tagged). Members are normalized to ISO2/regions. Contract `company_geo_revenue`, point-in-time by filing date. **Done:** SEC FSDS quarterly zips keep `segments` (whole history 2009q1-2026q3 landed in ~8 min): `company_geo_revenue` 847k rows, 3,816 filers with geographic revenue (660 non-US filers), 2,246 of our issuers linked (36 non-US-region via issuers.cik / exact name). Members -> ISO2 / regions (`app.linkage.members`, incl. composite and Spanish names). ESEF/CMF dimensional facts added where tagged (rare: 8 entities).
- [x] A6 **Company → company:** 10-K "major customer" disclosures (customers >10% of revenue) from `sec_text` sections, via rule/NER extraction (no LLM), linked by OrgLinker; plus GDELT co-mention edges from events_v2. Contract `company_links`. **Done:** `company_links` 6,152 customer links from 411 disclosing filers (6,049 XBRL MajorCustomers + 103 10-K text; 921 resolved to issuers, 1.7k government counterparties) + 14k GDELT co-mention monthly edges.
- [x] A7 **Spatial weight matrices** (`spatial_weights`, versioned, row-standardized and raw): contiguity, inverse distance, k-nearest, trade, IO, banking, co-mention, plus company-level exposure weights (revenue geography × country). Built with libpysal. **Done:** `spatial_weights` v20261012, 6.4M cells, all row sums = 1: country contiguity_queen/rook, inv_distance, knn5, trade/trade_exports (26 yrs), io_upstream (5 yrs + country_sector 2020), bank_claims (27 periods); issuer customer, comention; issuer_country exposure (FY2009-2026, ~1.6k issuers). API `app.linkage.weights.get_W(kind, level, as_of, standardize, fmt, ids)` / `list_W()`.

## B. Spatial econometrics (owner: event-models)
- [x] B1 Global Moran's I and LISA, plus Getis-Ord Gi* on event intensity, tone and attention by country, theme and week. Emerging-hotspot classification over time. Table `event_hotspots`.
  - done 2026-10-13 - `app/events_models/hotspots.py` (job event_hotspots, nightly, incremental by T1 week fingerprint); Moran/LISA permutation inference re-implemented in numpy (`app/modeling/_spatial.py`, matches esda; esda's numba JIT costs ~70 s/process), Gi*, ESRI-style emerging classes + Mann-Kendall.
- [x] B2 Spillover models: SAR, SEM, SDM and SLX, and spatial panel FE (spreg / linearmodels with W·X). Effect of event shocks in linked countries on equity index returns, volatility and macro surprises. Direct, indirect and total effects. Table `spatial_spillovers`.
  - done - `spillovers.py` (weekly event_spillovers): OLS-FE/SLX/SAR/SEM/SDM ML panels (two-way FE; matches spreg ML_Lag), LeSage-Pace effects with MC inference, per-country transmit/receive, LM tests; responses ret / range vol / macro-surprise change; W contiguity + inverse distance (linkage get_W, NE fallback).
- [x] B3 Diebold-Yilmaz connectedness (VAR-based; reuse Model Studio VAR) for country indices and sectors, rolling. Net transmitter/receiver. Table `connectedness`.
  - done - `connectedness.py` (nightly, incremental): rolling DY generalized FEVD (VAR(1) ridge, 200 d, step 5, H 10) for 42 country indices and 11 sector ETFs, returns + Parkinson vol, 2012->.
- [x] B4 Spatio-temporal: local projections of returns and volatility on event shocks plus spatially lagged shocks; spatial VAR. Tables `event_irf`.
  - done - `irf.py` (weekly event_irf): panel LP (own + W-lagged shock, Driscoll-Kraay) and pooled spatial VAR(1) IRFs with simulation bands (all units + 8 large markets).
- [x] B5 Add the spatial methods to **Model Studio**: spatial_lag, spatial_error, spatial_durbin, moran_lisa, connectedness, local_projection; dataset W selection; export.py support.
  - done - Studio methods spatial_lag / spatial_error / spatial_durbin / moran_lisa / connectedness / local_projection (`app/modeling/methods/spatial.py`, registry `_load`), W by kind in params, export via `_export_s.py` (embeds `_spatial.py` + the W used); tests/events_models.

## C. Event dynamics (owner: event-models)
- [x] C1 Multivariate Hawkes processes per (country × theme group): fit, branching ratio, **escalation probability** next 7/30 days, and cross-excitation (spillover to neighbours). Table `event_escalation`.
  - done - `hawkes.py` (nightly event_escalation): bivariate discrete-time Hawkes (country, W-neighbours) per theme group, EM + day-of-week background + beta grid, branching ratio, cross-excitation, NB-overdispersed MC escalation probabilities 7/30 d.
- [x] C2 Bayesian online change-point detection on attention and tone streams per label, built so it can also run online for real-time R1. Table `event_changepoints`.
  - done - `bocpd.py`: online `BOCPD` class (Normal-Gamma, truncated run length, to_dict/from_dict) + job event_changepoints (365 d window; hazard 1/100; calibrated ~0.13 false alarms per 200 d, 97% detection of 1.5 sd shifts).
- [x] C3 **Our geopolitical-risk index** per country from GDELT themes, validated against the published Caldara-Iacoviello GPR index (download). Table `gpr_index`. Labelled model-based.
  - done - `gpr.py` (nightly event_gpr): model-based GPR per country + GLOBAL; published GPRD/GPRC downloaded weekly; validation report data/logs/reports/gpr_validation_<date>.json (window still short - see state).
- [x] C4 **Event-study library:** CAR/BHAR by theme × country × sector from labelled shocks (reuse event_car), and an "expected reaction" lookup. Table `event_study_library`.
  - done - `event_study.py` (weekly event_study_library): car_study market model, country_index / sector_etf / issuer levels, windows 0,1 / 0,5 / -1,5 / 0,10; `expected_reaction()` fallback lookup (also in the API).
- [x] C5 Company event exposure: events in country X → company exposure via geographic revenue and supply-chain weights → an exposure score and expected impact (from the event-study library). Table `company_event_exposure`.
  - done - `exposure.py` (nightly event_exposure): linkage issuer_country exposure W (geo revenue) + customer links, home-country fallback; pressure score + shrunk expected 5-day CAR of active shocks.

## D. ML (after B/C; owner: event-models)
- [ ] D1 Gradient boosting with SHAP: next-week return and volatility per sector and country from event features (shocks, theme mix, spatial lags, exposures). Purged CV plus the label-permutation leakage test.
- [ ] D2 GNN (PyTorch Geometric on CPU, if feasible) on the company–country–sector graph. Ships only if it beats D1 and passes the permutation test.
- [ ] D3 Narrative tracker: multilingual sentence embeddings (CPU MiniLM) of story headlines → clustering into narratives, with narrative intensity indices.

## E. API + UI
- [ ] E1 API `/api/v1/events/models/...`: hotspots, spillovers, connectedness, escalation, changepoints, gpr, event-studies, company exposure, narratives.
- [x] E2 Global Events redesign (EVENTS_V2 E8) extended with: a hotspot map with significance, contagion network view, spillover dashboard, escalation probabilities, GPR index, narrative tracker, event-study library; plus Company 360 "event exposure".

## R1. Real-time layer (no Redis; owner: realtime)
- [x] R1.1 **Bus abstraction** (`app/realtime/bus.py`): publish/subscribe, consumer groups, offsets, replay, dead-letter. Implemented on SQLite WAL (`data/realtime/bus.sqlite`), with a Redis implementation stub for later. Retention/compaction.
- [x] R1.2 **Stream workers** (long-running, supervised by the worker process; heartbeats):
  - SEC EDGAR latest filings (Atom/JSON; 8-K, Form 4, 10-K/Q, 13D/G, 6-K): poll ~30–60 s within SEC fair-access limits
  - FCA NSM, TDnet, HKEXnews and DART recent disclosures
  - official central-bank and statistics press/RSS (Fed, ECB, BoE, BoJ, BLS, BEA, Eurostat, ONS)
  - **macro release capture**: at each scheduled time (W2 calendar), poll the official source until the actual appears, then compute the surprise immediately and alert
  - GDELT 15-minute updates (reuse the events_v2 Streamer) and Polygon news (REST polling)
- [x] R1.3 **Online enrichment:**
  - labelling (OrgLinker/themes)
  - story clustering
  - sentiment
  - insider-signal rescoring for new Form 4s
  - online change-point detection (C2)
  - alert evaluation
- [x] R1.4 **Live overlay + push:** an API layer merges hot-store values over the nightly snapshot (latest filings, events, macro actuals/surprises, alerts). An **SSE endpoint** `/api/v1/live/stream` (topics: filings, events, macro, alerts, news).
- [x] R1.5 **Lake persistence:** micro-batches compacted hourly into Parquet with knowledge_ts = first-seen (point-in-time), idempotent keys.
- [x] R1.6 **Ops:** stream lag/freshness/throughput on the System page, backpressure, restart policy, tests (bus semantics, replay, idempotency, recorded feed fixtures).
  - R1 result (realtime, 2026-10-13): `app/realtime/*` - SQLite WAL bus (+ Redis stub), 9 streams (edgar, fca_nsm, tdnet,
    hkex, dart [needs OPENDART_APIKEY], rss_official [12 official feeds], macro_capture, gdelt, polygon_news), consumers
    enrich (links/themes/stories/FinBERT), insider (Form 4 rescoring), changepoint (BOCPD; swaps to app.events_models
    when present), alerts (new_filing / insider_buy / macro_release / macro_surprise -> alert_events); hourly compaction to
    live_filings / live_news / live_events / live_macro / live_signals; API /api/v1/live/* (api.yaml 1.17.0) incl. SSE.
    Enable: `python -m app.cli realtime start` or `worker start --realtime` (default off). Tests: tests/realtime/*.
- [x] R1.7 **UI:** live ticker/feed, live tiles, alert toasts, "new filing" badges, per-stream health.
