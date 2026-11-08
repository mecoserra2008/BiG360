# Phase 3 integration backlog (orchestrator-maintained)

## API ↔ data
- [ ] Map `tech_setups` → API TradeSetup/indicators. Map `ms_*` / `swing_*` levels → `pivots`, and level families → the frontend LevelType (`technicals/taxonomy.FRONTEND_TYPE_OF_FAMILY`).
- [ ] Live predictions must use `app_state.model_promotions` (ML).
- [ ] The `mvp_us` / `mvp_global` universe screens return empty until `universe_membership` is populated (ref-identity).
- [ ] The filing-date anchored VWAP is disabled until `filings_index` issuer ids are resolved.

## Data
- [ ] Run `backfill_prices` for the MVP universe once ref-identity publishes `universe_membership`. Today only 329 securities have long history.
- [ ] Re-run US ingestion (sec_xbrl, sec_form4, sec_text) on the full MVP universe. The first runs used a 55-name fallback.
- [ ] Replace the provisional `P:<ticker>` ids in prices_daily after identity_resolve.
- [ ] Re-run technicals and the ML retrain after the backfill and after scoring lands.
- [ ] Polygon corporate actions: raise the page cap on busy weeks.

## Re-runs after fundamentals are re-normalized (ref-identity fixing AMZN FY2007, PETR4 ccy, debt nulls, shares, V missing)
- [ ] `moat --param mode=history`, then `forecast` (analytics-valuation). Re-check: wide share 10–20%, a US financials ERP bucket (needs ≥12 names), and whether the 1% ERP floor still binds for NA/JP.
- [ ] scoring (+ the 10y monthly history), then ML retrain on the full features, then technicals, then build_serving_db.

## Fixed by orchestrator (2026-10-11)
- [x] serving: USD duplicated in fx_latest → every USD security appeared twice in the screener
- [x] serving: universe filtered on valid_from (creation date > data date) → mvp screens were empty
- [x] core/schema: unstable unique() order in the enum error message
- [x] insider_transactions: 1,082 txn_ids stored in two region partitions (US default + issuer region). Cleaned up, plus a sticky-region guard in sec_form4._finalize. DQ gate passes again.

## Data issues still open (ref-identity)
- [~] BRK-B shares (bug-hunter): yfinance fallback now uses class-equivalent shares (marketCap/price = 2.14bn B-equiv, not 1.41bn class B). Fetched 2026-10-11, so PIT-visible from as_of >= 2026-10-11 only. Durable fix needs brka: custom-tag ingestion.
- [ ] PETR4 total_equity_parent swings between versions (481B ↔ 93B BRL)
- [ ] APAC/TW issuers (incl. 2330.TW) have no fundamentals; ~22% of securities lack market cap
- [ ] JP/LATAM shares missing at TTM period ends (47 issuers)
- [ ] insider_transactions.issuer_id is null in the table (signals-alt resolves it on the fly); identity_resolve should back-fill it

## Final bug-hunt list (from the frontend live verification, 2026-10-11)
- [x] API `CountryEventAgg.change_7d` (bug-hunter): was already a fraction; 162.2 = +16,220% because the prior week was a sparse back-fill (~500 vs ~85k events/day). Now null unless both 7-day windows are fully ingested; frontend heuristic removed.
- [x] API `CompanySentiment.sentiment_score` (bug-hunter): documented 0–100 percentile blend (api.yaml 1.3.0); frontend shows it separately from the −1…+1 index.
- [x] ADR market cap (bug-hunter): seeds/adr_ratios.csv (66 ADRs, ratios checked against ADR/home price), applied to securities_master.adr_ratio by ref_universe + identity_resolve.
- [x] PETR4 revenue (bug-hunter): not a CVM scale bug. yfinance served the USD 20-F numbers labelled BRL and CVM only covered 2024+. fundamentals_normalize now relabels a yfinance currency that matches another source's facts; CVM DFP/ITR back-filled for 2019–2023 (all 13 BR issuers).
- [x] GDELT headlines (bug-hunter): one slug cleaner (ids, extensions, wire prefixes, title case) shared by the linker and API; feed + company events deduped by normalized source URL.
- [x] System freshness (bug-hunter): per-table picker (SCD reference tables -> updated_at), sentinel dates ignored.
- [x] ISIN from CUSIP (bug-hunter): identity_resolve derives US/CA ISINs (identifier_map source derived_cusip, conf 0.95); serving coalesces them into SecurityRef.isin.
- [x] ML eval: fold rank_ic > 1 (it was a Sharpe stored as IC), compounding of overlapping long-short (now non-overlapping), fold windows added.
- [x] ML leakage audit: the 3M FT-Transformer FAILS the label-permutation test (IC +0.06–0.09 on shuffled labels; `row + lag1 + lag2` in FTTransformerCS.row_repr). Demoted. XGBoost (passes) is production for 1M/3M/6M. Guard `assert_no_label_leakage` runs on every training call.
- [ ] ML next: fix the Transformer architecture (gate or concat the lag path), retrain, promote only if it passes the permutation test AND beats XGB. Retrain the 1M/6M Transformer. Back-fill macro_betas history and tech_setups PIT history.
- [x] Insider Radar Unclassified (bug-hunter): identity_resolve fetches SEC submissions SIC for insider issuers without a sector (8 req/s) and re-classifies.

## Found during the bug-hunt verification (2026-10-11)
- [x] serving: security market_cap_usd fallback ignored adr_ratio, staleness and splits (TSM $11.7T, BRK-B $0.85B from 2015 class-A shares, 8766.T $6.4B). Now: own valuation row -> issuer primary-security valuation mcap -> price x shares only if shares <= 800 days old and known after the last split.
- [x] gdelt_link BRAND_CUES facebook/google contained literal backspace bytes instead of  (never matched). Fixed + test.
- [ ] fundamentals_normalize: TTM EPS sums quarters across a split (BKNG TTM 2026-03 = 157.38 mixes pre/post-split quarters). Per-share TTM should be split-adjusted (ref-identity).
- [ ] BRK-B: class-equivalent shares are PIT-visible only from as_of >= 2026-10-11 (yf fallback fetch date). Durable fix: ingest brka: custom tags (EPS per class A equivalent / average equivalent class A shares).
- [ ] ADRs whose ratio is not in seeds/adr_ratios.csv (~316 US-only ADRs without a home listing) still assume 1:1 in valuation/forecast (forecast flags adr_ratio_missing).

## Valuation follow-ups (after forecast v2: blended DCF+multiples, valuation_confidence)
- [ ] scoring: down-weight or ignore valuation upside when valuation_confidence < 40 (195 issuers)
- [ ] API/UI: expose fair_value_dcf / fair_value_multiples / range / tv_share / valuation_confidence / confidence_reasons. Show the headline upside as market-anchored and the raw DCF separately.
- [x] (bug-hunter) BKNG (1:25) / 8766.T (1:15) 2026 splits: forecast + cost_of_capital now split-adjust shares/EPS to the as_of price basis. BP.L: GBp handling verified correct for all 26 .L listings; BP FY2025 EPS $0.0034 is real (NI $55M) -> new flag eps_outlier instead of share_basis_mismatch when P/B is plausible. Still open (ref-identity): shares vs EPS inconsistent for 16 names (ANET, HON, SCHW, MCHP, MDT, FDX…). Shares taken from the annual report for 16 (KR/TW/IN/JP/LATAM).
- [ ] Review: NVDA is classed cyclical (semis), so its margin is normalized 61% → 32%. Confirm with the user.

## API follow-ups from signals-alt
- [ ] Join `insider_txn_scores` on txn_id (anomaly_score, cluster_id); read `news_items.relevance` and `gdelt_events.headline`

## API follow-ups from analytics-valuation
- [ ] Moat endpoint: read `moat.component_detail` / `moat_sources` instead of hard-coded thresholds (50/60).
- [ ] Forecast endpoint: use `forecast_valuation.sensitivity_grid` (it covers banks too); `roic_vs_wacc` from `component_detail._roic_vs_wacc`.
- [ ] Surface the forecast flags in the UI: erp_mode=implied (market-calibrated ERP), currency_mismatch, equity floored, rf_fallback.

## Frontend
- [ ] Re-generate `src/api/generated/schema.d.ts` from api.yaml v1.1.0+ and update the adapters to use the new fields: data_status, isin, peer_median, models[], yield_curves, etc.
- [ ] Add a stagflation regime UI state, and map the `partial` job status to a warning state.
- [ ] Show the technicals research result in the UI: level-type reliability, and "entry filter, not a strategy".

## Keys the user may add
- OPENDART_APIKEY (Korea: biggest unlock), KRX_OPENAPI_KEY, COMPANIES_HOUSE_APIKEY, OPENFIGI_APIKEY, BOK_ECOS_APIKEY, BANXICO_TOKEN.

## From LATAM follow-up (2026-10-11)
- [ ] ref-identity: normalizer keeps one reporting currency per issuer, so a mid-history switch drops the new-currency facts. Enel Chile switched CLP -> USD in FY2025 and its canonical data stops at FY2024. Support time-varying reporting currency (per fiscal year).
- [ ] BCI (Chile bank): no IFRS XBRL at CMF and no SEC filing, so it stays on yfinance.

## From EU expansion / price coverage (2026-10-12)
- [ ] prices_staging: concurrent detached yfinance writers appended duplicate (source_id,symbol,date) rows (457,755, removed). Make yf staging writes use upsert under the table lock (or have the finalize step dedupe), so parallel workers can't duplicate.
- [x] build_serving_db timed out at 60 min while heavy jobs ran in parallel; standalone it takes 4–7 min at 23k securities. Consider timeout_min 120 and running it only after the analytics chain is idle. → 2026-10-26: exclusive heavy slot + bounded builders + own DuckDB budget (OPERATIONS_ROADMAP "Refresh pipeline fixes 2026-10-26").
- [ ] Spain: only 8/72 eu_broad names have official FY2025; check the CNMV adapter for the broad universe. PL/GR/PT/LU/RO/HU/CZ: no national adapter (yfinance only).
- [ ] BaoStock IP ban: 424 Shanghai names are missing; resume with one worker when login works (data/state/price_coverage.json overview.resume). Nightly CN BaoStock jobs fail until then.

## From linkage (2026-10-13)
- [ ] When the detached Comtrade backfill finishes (~12 h, data/state/linkage.json), run `jobs run linkage_weights --force` so the trade matrices include Comtrade fills.
- [ ] Optional for the user: free COMTRADE_APIKEY (comtradedeveloper.un.org) to speed up refreshes; manually download the OECD ICIO 2025 zip (oe.cd/icio) into data/raw/linkage/icio/, then run linkage_io + linkage_weights. Scripts get HTTP 403 from oecd.org.
- [ ] Re-check the full test suite once event-models and realtime finish: 5 failures were seen mid-flight (test_api x1, test_api_serving x3, events_models expected_reaction_fallback).

## From the Global Events UI (2026-10-13) — small backend follow-ups
- [ ] events_v2: a country × theme endpoint (the heatmap currently makes one /map call per theme); stories filter min_sources; changepoint 
ame for issuer labels; escalation raw vs capped branching ratio; event-study daily CAR path (currently 4 windows).
- [ ] Theme precision 68% (crime vocabulary → conflict_military): refine the GKG theme map.
- [ ] live: /live/status should clear lag/freshness for stopped streams; alerts consumer needs live-evaluable rules to publish on the alerts topic.
- [ ] attention/tone z need ≥90 obs; they fill as the 2020 backfill lands (E6 awaiting the user's launch).

- [ ] TWD FX: only weekly Fed H.10 (FRED DEXTAUS), so it lags up to ~7 days; DQ fx_coverage tolerance is 10d for TWD (2026-10-13). Add the official CBC Taiwan daily NTD/USD rate as the primary source so TW close_usd is current.
- [x] forecast.write_forecasts DuplicateError on as_of_date (unaliased strftime group key) fixed 2026-10-13.

## From price-history backfill (2026-10-14)
- [ ] B3 COTAHIST pre-2005 unit errors (e.g. VULC3 0.00045 vs 45) now win over Yahoo by priority; review b3_cotahist.py scaling.
- [ ] 35 US securities where Yahoo history and Polygon don't join cleanly.
- [ ] 349 CN names priced only from Yahoo don't update nightly until BaoStock works again.

## From the price-consumer audit (2026-10-14)
- [ ] Row-count windows (scoring momentum, ML features, insider features) ignore calendar gaps >30d; rare (mostly illiquid BR). Consider a calendar guard.
- [ ] Yahoo FX scale glitches (86 jumps >3x across 12 pairs, e.g. USDVND=X 2013): add a scale-jump cleaner for FX instruments.
- [ ] usd_per_unit null for EUR pre-1999, ISK 2008-18, IDR pre-2005, TWD latest (stale): extend official FX history / gap rules.
- [ ] ML long-short benchmark: 5/65 month-ends before 2011-10 lack daily bars.
- [ ] price_cross alert can false-fire on split days with metric=close (use adjusted close).

- [ ] GDELT E6: day 2025-05-13 checkpointed with 512/576 files (network/DNS loss while the laptop slept). After E6 finishes, re-run days with files_missing > 0 using backfill --redo for those dates. Consider: don't checkpoint a day with missing files on connection errors (retry later instead).

- 2026-10-19 03:31: GDELT E6 backfill crashed with numpy MemoryError (98 MB alloc in simhash_many) while 3 agents ran serving builds/tests concurrently (system commit exhausted, not a code bug). Restarted 03:33 with .venv python (1,904 days left). 18 orphaned multiprocessing workers from 14-15 Oct backfills were killed. If it recurs: lower --dl-threads/workers while agents run heavy builds, or add a page-file check.

- (#7 smart money, 2026-10-19) Weekly sec_13f job still upserts via Polars; 2026 partition will reach ~10M rows by early 2027 -> switch to the DuckDB merge path used by sec_13f_backfill (OOM risk).
- (#7) USER DECISION: BaFin/Bundesanzeiger net-short pages are robots-disallowed (/nlp) and CONSOB is behind a captcha -> both skipped; DE/IT EU shorts missing. FINRA SI symbol resolution only 17%; float proxied by shares outstanding (~3.8k issuers).
- (#7) Smart-money combined score: no significant rank IC (2023-06..2026-10); treat as descriptive screen. Report data/logs/reports/smart_money_ic_20261016.json.

## From O2.2a data integrity (2026-10-20)
- [x] Yahoo FX scale glitches: 10x/100x/1000x flip pairs repaired in prices_finalize (FX region, 8 pairs / 30 bars); TWD close_usd as-of tolerance 12 d (attach_usd SLOW_FX_GAP_DAYS) until the CBC daily rate exists.
- [ ] usd_per_unit still null for EUR pre-1999 (no DEM legacy series in fx_rates), ISK 2008-18, IDR pre-2005: needs official legacy FX history.
- [ ] Review the 33 CN (BaoStock) identity breaks (long suspensions + restructurings / backdoor listings) and the ~70 unresolved 10^k jumps listed in data/logs/reports/prices_finalize_*.json (`unit_repairs.unresolved_top`); suppress via reference/seeds/price_identity_breaks.csv if a split is wrong.
- [ ] Euro IG OAS: not on FRED (only euro HY / EM ICE series); find an official source (ECB) before adding it to credit_spreads.

- 2026-10-20 (O1 filler) BUG: task id with "*" (gap_prices.CN_*) used as filename -> OSError Errno 22 every tick; filler ran nothing (E6 not resumed, JP refill not done). Fix first on resume.

- 2026-10-20 (O4) Data anomaly: screener shows ASML ROIC 606% -> check ROIC invested-capital denominator (IFRS/ESEF mapping), add plausibility cap/DQ rule.
- 2026-10-20 (O4) Licensing before any bank use: disable Yahoo/yfinance, AKShare, BaoStock; delete FRED-copied ICE BofA OAS archive (data/raw/fred/oas_archive) unless ICE licensed; choose licensed vendor via app.ingestion.vendors adapters.
- 2026-10-20 (O4) .NET 8 support ends Nov 2026 -> target .NET 10 LTS for the C# sample (one-line retarget in integration/dotnet).

- 2026-10-20 (perf) follow-ups: serve pre-built /openapi.json with ETag (515 KB rebuilt per page load); /live/news groups whole news table per call (200-450 ms each page); single shared SSE across tabs (BroadcastChannel) or HTTP/2 to remove 6-connection ceiling.

- 2026-10-20 (perf) Unexplained API child exit at 22:23:25 UTC (not a deliberate kill) -> check data/logs/app/api.err.log around then; supervisor restarted it.

- 2026-10-24 (BQL demo) Issues found: ROIC outliers (EXPE 1498%, NTAP 357%, RR.L 196%, ASML 606%) -> ROIC invested-capital denominator / sanity cap needed; si.pct_float null for all US large caps in BQL (join or field mapping broken); sm.z_smart_flow ~constant -0.14 across names (degenerate z); name column duplicated when requested explicitly; group().agg() output cannot sort(n). Demo queries in scratchpad/bql_demo/.

- [x] (fixed 2026-10-26: incremental mode=auto, see OPERATIONS_ROADMAP "Refresh pipeline fixes 2026-10-26") 2026-10-25 prices_finalize full run took ~2h53m (12:14-15:07; 98.5M rows, 24,950 securities) after the O2.2a chunked/streaming change -> too slow for the weekly window; optimise (parallel per region, incremental since last finalised date instead of full rewrite) and revisit the 150-min timeout.

- [x] (root cause fixed 2026-10-26: day-replace store + PK-deduping compaction + heal in events_agg + robust event_changepoints) 2026-10-25 event_daily_agg had 70,022 duplicate PKs for 2026-05-22..31 (one file m-202605.parquet; append of recomputed rows, likely gap-filler gdelt_redo or live finalise rollup writing with write_mode append) -> dq_checks failed -> snapshot blocked; also caused event_changepoints "duplicate labels". Deduped keep=last (backup data/backups/event_daily_agg_m-202605_pre_dedupe_20261025.parquet). ROOT-CAUSE FIX NEEDED: writers of event_daily_agg must upsert/overwrite per date partition, not append.

## From the refresh-pipeline fixes (2026-10-26)
- [ ] ev2_stories serves ~6.6 M stories (most of them single-article stories of the last 120 days with salience >= 2.2): the snapshot grew by ~2 GB. Consider a stricter salience bar or a per-day cap for unlinked singletons (events-v2 decision; the build is now bounded either way).
- [ ] conformal_envelopes still recomputes ~4.5 y for every region (~70-80 CPU-min at 25k securities, ~40 min wall with 2 workers). Plain-mode calibration (production radii) only needs the audit / emitted origins; the 600-session replay is for the ACI variants. Revisit with the conformal owner before changing model outputs.
- [ ] prices_finalize mode=full is still ~2-3 h at 98 M rows (the build itself, ~100 s per million rows); only needed for back-fills.
- [ ] Serving `meta` (freshness of every lake table) takes ~75 s: could use Parquet metadata / the register_lake counts.

## From the completeness audit (2026-10-27, workstream C; docs/DATA_COMPLETENESS.md)
- [x] 2026-10-24 BQL-demo issues fixed: si.pct_float (never computed -> short interest / shares outstanding), sm.z_smart_flow (robust median / MAD z), ROIC outliers (USD NOPAT / local IC mix + Yahoo "Invested Capital" chain + plausibility bounds roic_flag), duplicate name column, group().agg() sort(n).
- [x] FINRA SI: compact class-share / ETF / OpenFIGI / FTD-history symbol resolution; ADR short interest kept on the US line (was on the home listing via primary_security_id); market class SC -> XNAS. The "17 %" was of all FINRA rows (OTC included); US exchange listings 93.4 % -> 96.8 %.
- [x] factor_scores.delta_1m null on every as-of between month-ends (reference row 20..50 days back, either side).
- [x] US sector mapping: SIC backfill for every CIK issuer (US sectors 27 % -> 77 %); KSIC / CVM keyword rules extended (452 KR + 77 LATAM issuers; next ref_universe run).
- [x] Smart-money overview rows carried only their own dataset's fields (13F lists had null SI / score).
- [ ] earnings_events: map TDnet kessan-tanshin quarterly actuals (JP eps / rev surprises 0.1 %) (earnings owner).
- [ ] Universe full keeps ~760 delisted / acquired US names (no bar since <= 10-13): retire them (ref-identity).
- [ ] Statements for the remaining ~3k US small caps (sec_xbrl_bulk all_ciks) and shares outstanding for JP / KR / CN / HK / APAC prices-only names (market cap 17 % of universe full).
- [ ] HK sectors (HSICS not free): Yahoo profile sector for HK; JP / CN ISINs (JPX / CSDC lists).
