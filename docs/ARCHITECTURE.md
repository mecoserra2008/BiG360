# BiG Equity 360 — System Architecture

Status: v1 (Phase 0 system design, 2026-10-10). Scope and stack are set in `PLAN.md`.
This document is normative. Where it and a YAML contract disagree, **the contract wins**. Fix the doc in the same change.

Contents
1. Principles
2. Component diagram & processes
3. Data zones
4. Storage layout (Parquet + DuckDB)
5. Contracts index
6. Identifiers & entity resolution
7. Point-in-time (PIT) rules
8. Multi-currency & multi-GAAP
9. Ingestion: adapters, incremental updates, caching, rate limits
10. Job DAG, schedule & runtimes
11. Analytics pipelines (summary specs)
12. Serving layer & performance budget
13. Data quality scoring
14. Error handling & retries
15. Logging & observability
16. Testing strategy
17. Security
18. MVP slice & scale-up path
19. Module ownership & build phases
20. Decisions (trade-offs)
21. Open questions

---

## 1. Principles

* **Local and single-user.** Two Python processes (API + worker) and a Vite dev server. No message broker and no DB server.
* **Contracts first.** Every table has a YAML contract (`contracts/schemas/`). Every writer calls `app.core.storage.write_table()`, which validates against the contract. Each table has exactly one writer module.
* **Lake + snapshot.** Parquet (Hive-partitioned) is the system of record. The API reads only an immutable DuckDB **serving snapshot** that is rebuilt at the end of every nightly run. Readers and writers never contend.
* **Point-in-time everywhere.** Every fact carries a `knowledge_date`. Restatements are new rows; nothing is overwritten in a way that loses history.
* **Source-agnostic.** All sources plug in through the `SourceAdapter` interface (`backend/app/ingestion/base.py`). Region-specific quirks stay inside the adapter and the canonical-mapping YAML.
* **Polars for transforms, DuckDB for SQL and serving.** pandas is used only where a library needs it (yfinance, statsmodels).
* **CPU-only ML.** Model sizes, batch sizes and schedules fit 20 threads and 32 GB RAM. There is no GPU.

## 2. Component diagram & processes

```
                         ┌──────────────────────────── External sources ────────────────────────────┐
                         │ SEC EDGAR (XBRL facts, submissions, Form 4, 13F, docs)  FINRA SI          │
                         │ ESEF filings.xbrl.org  EDINET  OpenDART  CVM  AKShare  Companies House    │
                         │ Polygon/Massive  yfinance  FRED/ALFRED  ECB  DBnomics  World Bank  OECD   │
                         │ IMF  BoK ECOS  GDELT (Events, GKG, DOC)  OpenFIGI  GLEIF  HF model hub    │
                         └───────────────▲──────────────────────────────────────────────────────────┘
                                         │ httpx (rate-limited, retried, disk-cached)
┌────────────────────────── WORKER PROCESS (python -m app.scheduler.worker) ─────────────────────────────┐
│  APScheduler ──► DAG runner (contracts/jobs.yaml) ──► job modules: run(ctx) -> JobResult               │
│                                                                                                         │
│  ingestion/*  ──raw──► data/raw/<source>/...   (immutable landing, json.gz / zip / csv)                 │
│       │ normalize() ─► normalized/* Parquet ──► normalize/identity, fundamentals, prices                │
│       ▼                                                                                                 │
│  features/*  (ratios, sector_kpis, valuation_daily, sentiment_daily, tech_levels, ...)                  │
│       ▼                                                                                                 │
│  scores/*  (peer/factor/composite, moat, forecasts, insider_signals, macro_*, confluence_zones)         │
│  ml/*      (ml_features, ml_labels, model_runs, ml_predictions, model_eval_series)  + models_store/      │
│       ▼                                                                                                 │
│  build_serving_db ─► data/db/serving_<run_id>.duckdb ─► atomically flip data/db/CURRENT                 │
│  dq_checks ─► ops/dq_results     job bookkeeping ─► ops/job_runs     alerts_eval ─► app_state.duckdb    │
└───────────────────────────────────────────────────────────────────────────────────────┬─────────────────┘
                                                                                        │ read-only
┌────────────────────────── API PROCESS (uvicorn app.api.main:app, 127.0.0.1:8000) ─────▼─────────────────┐
│  FastAPI routers (contracts/api.yaml) ── deps.get_db() ──► serving snapshot (DuckDB, read-only)         │
│                                       └─ app_state.duckdb (watchlists, alerts; read/write)              │
│  POST /system/jobs/{id}/trigger and /models/train ──► enqueue file data/state/queue/*.json (worker polls)│
└───────────────────────────────────────────────────────────────────────────────────────▲─────────────────┘
                                                                                        │ JSON over HTTP
┌────────────────────────── FRONTEND (frontend/, Vite + React + TS) ────────────────────┴─────────────────┐
│  Market & Macro · Sector Explorer · Screener/Ranking · Company 360 · Insider Radar · Global Events       │
│  Model Lab · Watchlist & Alerts · System status      (types generated from contracts/api.yaml)          │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Processes**

| Process | Command (from `backend/`) | Writes | Reads |
|---|---|---|---|
| API | `..\.venv\Scripts\python.exe -m uvicorn app.api.main:app --port 8000` | `app_state.duckdb`, `data/state/queue/` | serving snapshot, app_state |
| Worker | `..\.venv\Scripts\python.exe -m app.scheduler.worker` (platform agent builds it) | everything under `data/` except app_state | Parquet lake, warehouse.duckdb |
| CLI | `python -m app.cli run-job <job_id> [--as-of D] [--param k=v]` | same as worker | same as worker |
| Frontend | `npm run dev` in `frontend/` (port 5173, CORS allowed) | none | API |

The API never runs jobs in-process. To trigger a job it writes a JSON request into `data/state/queue/`, and the worker polls that directory every 5 s. This keeps heavy CPU work (FinBERT, Transformer training) out of the request path.

## 3. Data zones

| Zone | Dir | Content | Mutability |
|---|---|---|---|
| raw | `data/raw/<source_id>/<dataset>/dt=YYYY-MM-DD/` | Payloads exactly as fetched (json.gz, zip, csv, html) | Immutable. Kept 400 days, except SEC docs and filings, which are kept forever. |
| reference | `data/reference/` | issuers, securities_master, listings, identifier_map, universe_membership | Snapshot overwrite |
| normalized | `data/normalized/` | Source data in contract shape: prices, filings, fundamentals_raw/canonical, insiders, news, GDELT, macro, FX | Upsert, PIT-versioned |
| features | `data/features/` | Derived per-entity metrics: fundamentals_wide, ratios, sector_kpis, valuation_daily, sentiment_daily, tech_levels, level_type_stats, gdelt_company_daily, filing_text_changes | Upsert / overwrite |
| scores | `data/scores/` | Opinions: peer/factor/composite scores, moat, forecasts, insider_signals, macro regimes/betas, confluence zones | Upsert |
| ml | `data/ml/` | ml_features, ml_labels, ml_predictions, model_runs, model_eval_series | Upsert |
| ops | `data/ops/` | job_runs, dq_results | Upsert / append |
| app_state | `data/db/app_state.duckdb` | watchlists, alerts, alert_events | Read/write by the API |

Flow: **raw → normalized → features → scores/ml → serving snapshot**. A zone only reads from earlier zones. The one exception is that ML features read scores.

## 4. Storage layout

```
STOCK SCANNER/
├─ .env                     keys (never committed / printed)
├─ contracts/               YAML contracts (this design)
├─ models_store/<model_run_id>/   model artifacts (xgb json, torch state_dict, scaler, feature list)
└─ data/                    (git-ignored)
   ├─ raw/<source_id>/<dataset>/dt=YYYY-MM-DD/<name>.json.gz
   ├─ cache/http/<source_id>/<aa>/<sha1>.bin|.json      HTTP response cache
   ├─ reference/{issuers,securities_master,listings,identifier_map,universe_membership}/part-*.parquet
   │  └─ seeds/ mvp_us.csv, mvp_global.csv, manual_overrides.csv     (ref-identity owned inputs)
   ├─ normalized/prices_daily/region=US/year=2026/part-<uuid>.parquet
   ├─ normalized/fundamentals_raw/source_id=sec_edgar/fiscal_year=2025/part-*.parquet
   ├─ normalized/... features/... scores/... ml/... ops/...      (path = contract storage.path)
   ├─ db/warehouse.duckdb          worker-side views over all Parquet tables + scratch tables
   ├─ db/serving_<run_id>.duckdb   immutable serving snapshot (last 3 kept)
   ├─ db/CURRENT                   text: file name of the live snapshot
   ├─ db/app_state.duckdb          watchlists / alerts
   ├─ state/                       per-source cursors (<source_id>.json), job queue/
   ├─ logs/                        api.log, worker.log, jobs/<job_id>/<run_id>.log
   ├─ tmp/duckdb/                  DuckDB spill directory
   └─ .locks/<table>.lock          per-table writer locks
```

**Parquet conventions** (implemented in `app/core/storage.py`)
* Hive partitions `col=value`. Partition columns are **not** stored inside files. They are re-attached on read with contract dtypes.
* Files are `part-<uuid12>.parquet`, zstd-compressed, with statistics.
* `write_table(df, table, mode)` modes: `upsert` (default: merge the touched partitions on PK, new rows win), `append`, `overwrite`, `overwrite_partition`. Every partition write is atomic: write to a tmp dir, rename the old one away, rename the new one in, then delete the old (retried on Windows `PermissionError`). Writers are serialized per table with a `filelock`.
* Partition size target is 5–200 MB. Partitioning in the contracts is chosen for that: by `year` for daily tables, by `source_id/fiscal_year` for raw facts, by `year_month` for news and events.
* Readers: `scan_table()` gives a typed Polars LazyFrame. `register_views(con)` creates one DuckDB view per contract and a typed empty view if no data exists yet, so SQL never breaks on a fresh install.

**DuckDB files**
* `warehouse.duckdb` (worker only) holds views for all Parquet tables plus scratch/materialized helper tables that jobs may create (names prefixed `tmp_`).
* `serving_<run_id>.duckdb` is a fully materialized copy of what the API needs (§12). It is built by the `build_serving_db` job (owner: api), then `publish_serving_db()` rewrites `CURRENT` atomically. The API checks `CURRENT` on each request through `deps.get_db()` and reopens when it changes. This works on Windows because the API never holds a handle on a file the worker must replace. The last 2 snapshots are kept (KEEP_SNAPSHOTS = 2, O1.7; CURRENT is never deleted) and older ones are deleted when not locked.

## 5. Contracts index

| File | Consumers | Purpose |
|---|---|---|
| `contracts/schemas/*.yaml` (48 tables + `_conventions.yaml`) | `app.core.schema`, all writers, API serving builder | Columns, types, PK, partitioning, PIT semantics, owner |
| `contracts/canonical_line_items.yaml` (112 items) | normalize.fundamentals, analytics | Canonical vocabulary + tag fallback chains (US-GAAP, IFRS, EDINET, DART, CAS labels, CVM codes) + derivations |
| `contracts/sector_taxonomy.yaml` (11 sectors / 79 subsectors) | normalize.taxonomy, API meta | Own taxonomy + mappings from yfinance industry, SIC ranges, TSE33, KSIC, CSRC, CVM, overrides |
| `contracts/sector_kpis.yaml` | analytics.ratios/sector_kpis/scoring, API | 48 generic metrics, 5 accounting scores, 28 KPI sets (each subsector in exactly one) with quality weights and peer metrics |
| `contracts/scoring.yaml` | analytics.scoring, moat, forecast, API screener | Metric sources, transforms, peer groups, self-history, market-cap adjustment, factors, composite profiles, missing-data policy, cost of capital, moat rules |
| `contracts/api.yaml` | API agent, frontend (TS type generation) | OpenAPI 3.1, 61 operations |
| `contracts/jobs.yaml` | scheduler, API system router | Job registry + DAG + schedule + runtime estimates |

`backend/tests/test_contracts.py` checks every cross-reference: PK and partition columns exist, every subsector belongs to exactly one KPI set, weights sum to 1, factor members resolve, DAG is acyclic, API `$ref`s resolve. **Any contract change must keep it green.**

## 6. Identifiers & entity resolution

**Internal ids** (see `_conventions.yaml`)
* `issuer_id = "I" + base32(sha1(seed))[:12]`. The seed is the first authoritative id found, in this preference order: `lei:` > `cik:` > `edinet:` > `dart:` > `cvm:` > `cn:` (exchange:code) > `hk:` > `ukch:` > `isin:` > `name:<country>:<norm name>`. The id is **assigned once and persisted**, never recomputed. If two issuer ids are later found to be the same entity, the older id survives, and the newer one is recorded in `identifier_map` as `id_type=issuer_alias` and in `issuers.merged_into_issuer_id`.
* `security_id = "S" + base32(sha1(issuer_id|share_class_key))[:12]`. There is one per economic share class. ADRs/GDRs are separate securities with `underlying_security_id` and `adr_ratio`.
* `listing_id = "L" + base32(sha1(security_id|mic))[:12]`. Ticker changes add a row to `listings` with `valid_from/valid_to` and keep the same `listing_id`.

**External id namespaces** (`identifier_map.id_type`): cik, ticker_mic (`SYMBOL@MIC`), yf_ticker, polygon_ticker, isin, cusip, figi/composite_figi/share_class_figi, lei, edinet_code, jp_sec_code, dart_corp_code, kr_stock_code, cvm_code, cnpj, cn_code, hk_code, uk_company_number, permid, name_country, gdelt_org_name, issuer_alias. Each mapping has validity dates, `source_id` and `confidence`.

**Resolution pipeline** (`app.normalize.identity`, owner ref-identity)
1. **Seeds.** The universe seeds come from SEC `company_tickers_exchange.json` (CIK↔ticker↔exchange), the JPX listed-company list (code + TSE33), the DART `corpCode.xml` (corp_code↔stock_code), CVM `cad_cia_aberta` (CVM code↔CNPJ), ESEF filer LEIs, the HKEX/SSE/SZSE lists via AKShare, and the curated `data/reference/seeds/mvp_global.csv`.
2. **Registry joins.** Exact joins on registry ids give confidence 1.0.
3. **OpenFIGI** (`/v3/mapping`, up to 100 jobs/request with a key) maps ticker+exchange code or ISIN to FIGI, composite FIGI, share-class FIGI and security type. This gives cross-listing linkage (composite FIGI) and ADR detection.
4. **GLEIF** LEI↔ISIN relationship file (daily, free) bridges ESEF (LEI-keyed) to listings (ISIN).
5. **Fuzzy fallback.** rapidfuzz `token_set_ratio` ≥ 92 on normalized name *and* the same country gives confidence 0.85. These mappings are **not** used for fundamentals until confirmed (manual override CSV), but may be used for news linking.
6. **Back-fill.** Adapters write source-native keys (`source_entity_id`). The `identity_resolve` job fills `issuer_id`/`security_id` on new normalized rows. Rows that cannot be resolved stay null, are counted in `dq_results`, and are listed on the System page.

**Primary security for scoring.** Use the issuer's home listing if it is in the universe, otherwise the most liquid listing (ADR). Scores and ML use one security per issuer (`is_primary_security`). Prices are stored only for each security's primary listing.

## 7. Point-in-time (PIT) rules

* `knowledge_date` is mandatory on fundamentals (raw, canonical, wide, ratios, KPIs), filings, insider transactions, news, GDELT, macro observations, 13F and short interest.
* **Sources of knowledge_date:**

  | Source | knowledge_date |
  |---|---|
  | SEC | `filed` date, with `acceptanceDateTime` stored in `knowledge_ts` |
  | EDINET | `submitDateTime` |
  | DART | `rcept_dt` |
  | ESEF | filing `date_added`/`processed` (conservative), or the publication date if present |
  | CVM | `DT_RECEB` |
  | FRED | ALFRED `realtime_start` |
  | GDELT | `DATEADDED` |
  | Form 4 | filing date (never the transaction date) |

* **Imputed knowledge dates** (`knowledge_date_estimated=true`) apply to sources with no publication date (yfinance fundamentals, some AKShare data, non-vintage macro):

  | Case | Lag after period end |
  |---|---|
  | US quarterly | +45 d |
  | US annual | +75 d |
  | Non-US quarterly/half-year | +60 d |
  | Non-US annual | +120 d |
  | Macro | `release_lag_days` from `macro_series` |

* **As-of query.** For date D and versioned facts, take the row with `max(knowledge_date) <= D` per natural key. Analytics must use the helper `app.normalize.pit.asof(lf, D, keys)` (ref-identity provides it), not ad-hoc filters.
* **Labels.** Forward returns run from close(D+1) to close(D+1+h), so information released after the close on D never leaks into the label window.
* **Universe.** `universe_membership` keeps delisted securities (survivorship-free). Delisting returns follow the `ml_labels` rule.
* **Known limitation.** Sector classification is current-only. The look-ahead from reclassification is minor and accepted (see §20).

## 8. Multi-currency & multi-GAAP

**Currency**
* `fx_rates` stores daily USD per unit. Precedence: ECB reference rates (EUR crosses converted via EURUSD) > FRED H.10 (`DEX*`) > yfinance `XXX=X`. Gaps are forward-filled up to 5 days with `source_id=filled`.
* **Balance-sheet (instant) items** use the rate at `period_end`. If there is no rate that day, use the last rate on or before it.
* **Flow items** use the period average: the mean of daily rates over `[period_start, period_end]`, from `app.normalize.fx.period_average`.
* **TTM in USD** is the sum of the discrete quarters' USD values, not the local TTM times one rate.
* `fundamentals_canonical` stores `value` (reporting currency), `value_usd`, `fx_rate`, `fx_method` and `fx_source`.
* **Prices** are stored in trading currency. `listings.price_divisor` normalizes GBp/ZAc to GBP/ZAR. `close_usd` is converted at that day's rate.
* **Valuation ratios** (`valuation_daily`) are always computed **USD-on-USD**. Market cap = PIT shares × close × fx(date). Fundamentals use the USD basis. This removes the trading-vs-reporting currency mismatch for ADRs and dual listings.

**GAAP**
* `fundamentals_raw.taxonomy` records the namespace, and `canonical_line_items.yaml` holds the per-taxonomy tag chains.

| GAAP | Notes |
|---|---|
| US-GAAP | Base case. |
| IFRS (`ifrs-full`) | ESEF, SEC 20-F, K-IFRS, JMIS/IFRS Japanese filers (`jpigp_cor`). There is no mandated operating subtotal, so `ebit` is derived. IFRS 16 leases inflate EBITDA versus US-GAAP operating leases; use `net_debt_incl_leases_to_ebitda` and the lease-aware EV note. |
| J-GAAP (`jppfs_cor`) | Ordinary income is kept as its own item. `jppfs_cor:ShareholdersEquity` is **not** total parent equity. Intangibles include goodwill. |
| K-IFRS | Consolidated (CFS) is preferred over separate (OFS). `dart_OperatingIncomeLoss` is used for operating income. |
| CAS (China) | Chinese statement labels. 营业利润 includes investment income (dq penalty). Dividend cash line includes interest. |
| BR (CVM) | Account codes per DFP/ITR plan for non-financials. Expenses are negative in the source, so normalize the sign. Scale `MIL` ×1000. |

* **Fiscal-year label.** `fiscal_year` = calendar year in which the fiscal year **ends** (Walmart FY ending Jan-2025 → 2025; Toyota FY ending Mar-2025 → 2025). Quarters inherit the fiscal year of the FY that contains them.
* **Source precedence per issuer** (applied per item/period; lower sources are used for agreement checks):

  | Region | Order |
  |---|---|
  | US | `sec_edgar` > `yfinance` |
  | EU / UK | `esef_xbrl_org` > `sec_edgar` (20-F) > `companies_house` > `yfinance` |
  | JP | `edinet` > `sec_edgar` > `yfinance` |
  | KR | `opendart` > `sec_edgar` > `yfinance` |
  | CN / HK | `akshare` > `sec_edgar` > `yfinance` |
  | LATAM | `cvm` (BR) > `sec_edgar` (20-F: MX/CL/AR/PE/CO) > `yfinance` |

## 9. Ingestion

**Adapter interface** (`app/ingestion/base.py`): `discover()`, `fetch(since, entities)`, `normalize(fetched)`, `run()`. The docstring carries the binding rules: own partitions only, source-native keys, mandatory `knowledge_date`, no secrets in logs, idempotent fetch. Adapters register with `@register_adapter`. The detailed endpoint research is in `docs/DATA_SOURCES_AMERICAS_EUROPE.md` and `docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md`, written by the research agents. If those documents contradict a rate limit below, **the research docs win** and `DEFAULT_LIMITS` in `app/core/ratelimit.py` must be updated.

**Incremental update strategy**

| Class | Cursor | Strategy |
|---|---|---|
| SEC filings | `job_runs.watermark` = last processed accession date | Daily form index (`/Archives/edgar/daily-index/`). Fetch companyfacts **only** for CIKs with new 10-K/10-Q/20-F/40-F/6-K. The weekly bulk `companyfacts.zip` (~1.3 GB) reconciles anything missed. |
| Form 4 | daily index | Parse the new Form 4/4A XML only. |
| ESEF / EDINET / DART / CVM | publication date | List filings since the cursor, then fetch only those. EDINET documents list per day. DART `list.json` by `bgn_de`. CVM yearly zip files are re-downloaded weekly (ETag/Last-Modified via cache). |
| Prices US | last loaded trading date | Polygon grouped-daily, 1 call per missing date. yfinance fills long history once (backfill) and validates. |
| Prices global | last date per listing | yfinance `download()` in batches of 50–100 tickers, 7-day overlap to catch revisions and corporate actions. |
| FX / macro | last observation per series | FRED `observation_start=cursor-30d`. ALFRED vintages for the core series. Others re-fetch the last 2 years. |
| GDELT | last 15-min file processed | Read `lastupdate.txt`, fetch new export/GKG files, stream-filter and discard. |
| News | `published_at` max | Per source. |

Per-entity cursors live in `data/state/<source_id>.json`. The job-level cursor lives in `job_runs.watermark` and is committed only when the job ends with success or partial.

**HTTP caching** (`app/core/http.py`): an on-disk cache keyed by method+URL+params, with secret params excluded from the key. TTLs:

| Data | TTL |
|---|---|
| Immutable filed documents (SEC archives, EDINET docs, DART zips) | forever (`-1`) |
| Lists and indexes | 1–6 h |
| Price/quote endpoints | 0 |

The raw zone is the durable copy, and the cache is disposable. Run `rm -r data/cache` any time.

**Rate limits** (process-wide token buckets in `app/core/ratelimit.py`; Retry-After honoured; HTTP 429 drains the bucket)

| Source class | Limit used | Notes |
|---|---|---|
| SEC EDGAR | 8 req/s, burst 8 | Fair-access cap is 10/s. `User-Agent` = `SEC_USER_AGENT` from .env (required). Prefer bulk zips. |
| Polygon/Massive free | 1 call / 12 s | 5/min. Use grouped-daily (all US tickers per call). The 2-year backfill takes ~500 calls, about 100 min, as a one-off manual job. |
| yfinance | 0.5 req/s, burst 2 | Unofficial. Batch tickers. Back off for 10 min after repeated errors. |
| FRED / ALFRED | 1.5 req/s | Documented 120/min. |
| GDELT DOC API | 1 call / 5 s | Used only for on-demand company timelines. |
| GDELT files | 4 req/s | Static files. 96 files/day per stream. |
| EDINET / OpenDART / ESEF / CVM | 1–2 req/s | DART also has a 20,000 calls/day cap, counted by the adapter. |
| AKShare (Eastmoney/Sina) | 1 req/s | Fragile scraping. Isolate failures per ticker. |
| OpenFIGI | 20/min (no key), ~4/s with key | Up to 100 jobs per request with a key. |
| ECB, DBnomics, World Bank, OECD, IMF, BoK ECOS, FINRA | 0.5–2 req/s | |

## 10. Job DAG, schedule & runtimes

Machine-readable source: `contracts/jobs.yaml`. Timezone is America/New_York.

* **nightly**: Tue–Sat 01:30 ET (≈06:30 Lisbon). Processes the previous US session. Asia and Europe sessions for that date are already closed.
* **weekly**: Saturday 06:00 ET, waits for nightly.
* **monthly**: first Sunday 08:00 ET.
* **manual**: backfills and on-demand training.

Nightly DAG (MVP runtime estimates in minutes; jobs on the same row run in parallel; worker pool of 6 concurrent jobs):

```
t=0   ref_universe(3)  fx_rates(1)  macro_fred(3)  macro_global(5)  sec_filings_index(3)  esef(8) edinet(8) opendart(8) cn_fund(10)  gdelt_events(10)
        │                                            │                    ├─ sec_xbrl_facts(3)
        ├─ prices_us_polygon(1) prices_global_yf(6) corporate_actions(3) ├─ sec_form4(5)
        │        └──────────────┬──────────────┘                         └─ sec_text(5)
        │                 prices_finalize(2)
        ├─ news_ingest(8)  gdelt_gkg(25)
        ▼
      identity_resolve(3)  [needs all entity-bearing ingests]
        ├─ fundamentals_normalize(8) ─► ratios_kpis(3) ─┐
        │                         └─► valuation_daily(2)┤─► forecast(5) ─┐
        ├─ insider_signals(3)                            │                │
        ├─ nlp_sentiment(10) ─┐                          │                │
        ├─ lazy_prices(5)  ───┼─► sentiment_daily(2) ────┤                │
        └─ gdelt_company_daily(3)┘                       │                │
      macro_regime_nowcast(1) ───────────────────────────┴─► scoring(5) ◄─┘
      tech_levels(3) ─► confluence_zones(2)                     │
                                                    ml_features(3) ─► ml_predict(2) ─► composite_ml_blend(1)
                                                                                          │
                                                          dq_checks(2) ─► build_serving_db(3) ─► alerts_eval(1)
```

Critical-path estimate (MVP, ~700 securities): about **75 min** (the GKG stream dominates) and ≤ 2 h worst case. At full scale (~15k securities) about 4–5 h. That still finishes before the European open.

Weekly: sec_xbrl_bulk(20), sec_13f(15), finra_short_interest(2), cvm(8), uk_companies_house(5), yf_fundamentals(10), level_type_backtest(30), macro_regime_fit(5) → macro_betas(5) → moat(3), insider_models_fit(10), ml_labels(3) → ml_train_xgb(20) + ml_train_transformer(90) → ml_ensemble(5). About 3.5 h at MVP. The Transformer job has a 480-min timeout.

Monthly: universe_rebalance, openfigi_refresh. Manual: backfill_prices (Polygon 2 years plus yfinance max history), backfill_fundamentals, rebuild_features.

**Initial bootstrap** (one-off): `ref_universe` → `backfill_prices` → `sec_xbrl_bulk` + global filings (`since=None`) → `identity_resolve` → `fundamentals_normalize` (full history) → `rebuild_features` → weekly DAG → nightly DAG. Estimate 6–10 h at MVP, dominated by the Polygon rate limit and EDINET/DART document downloads.

## 11. Analytics pipelines (summary specs; details live in the YAML contracts)

* **Fundamentals normalization** (ref-identity):
  1. Select raw facts: no dimensions, consolidated.
  2. Map them to canonical items by tag chain, honouring source precedence (§8).
  3. Normalize signs.
  4. Derive discrete quarters, halves and TTM.
  5. Apply derivations in dependency order (`app.normalize.formula`, an AST-whitelisted evaluator also used by analytics).
  6. Apply FX.
  7. Run accounting identity checks (assets ≈ liabilities + equity ±2%, CFO+CFI+CFF ≈ Δcash ±5%).
  8. Write `fundamentals_canonical`, then pivot to `fundamentals_wide`.
* **Ratios and sector KPIs** (analytics-scoring): evaluate `sector_kpis.yaml` formulas on TTM and FY rows per issuer, PIT-versioned. Accounting scores: Piotroski F, Altman Z'' (all non-financials) plus original Altman Z (manufacturers), Beneish M, Sloan accruals.
* **Scoring** (analytics-scoring):
  1. Compute per-metric peer percentile within the peer group, falling back subsector×region×size → subsector×region → subsector → sector×region → sector until there are at least 8 peers.
  2. Compute a self-history z-score (5–10 years of TTM quarterly observations, at least 8).
  3. For valuation metrics, compute a market-cap/quality-adjusted residual percentile.
  4. Blend these into a metric score, roll up to 10 factor scores (with KPI-set overrides for banks, insurers and REITs), then to composite profiles.
  5. Custom screener weights are applied on the fly in SQL.
* **Moat** (analytics-valuation): 7 weighted components (ROIC−WACC spread and persistence, gross-margin level and stability, margin premium, subsector revenue share, intangibles/R&D intensity). Classification rules and confidence are in `scoring.yaml`.
* **Forecast** (analytics-valuation): 5-year driver model (growth fades toward a sector median and GDP; margins mean-revert to the 5-year own/peer blend; capex/D&A/NWC as % of revenue; tax normalized). The 3 statements are linked (cash and debt balance via a revolver plug). Bear, base and bull = 10th/50th/90th percentile driver paths from the own and peer historical distributions. FCFF DCF with the `scoring.yaml` cost of capital and terminal growth = min(local 10y nominal GDP consensus proxy, 3%). Reverse DCF solves the implied 5-year revenue CAGR (base margins) and the implied steady-state FCF margin. Financials use an excess-return (ROE−CoE) model instead.
* **Insider** (signals-alt): open-market buys and sells only, with Cohen–Malloy–Pomorski routine classification. Features: value / market cap, number of insiders, role weights, holding change, 10b5-1 flag, recent price drawdown. IsolationForest gives the anomaly score; XGBoost gives P(3M excess > 0), trained weekly with the same purge/embargo as ML. The signal score is a 0–100 blend; no activity = 50.
* **NLP** (signals-alt): `ProsusAI/finbert` for English titles and snippets. A multilingual model (e.g. `cardiffnlp/twitter-xlm-roberta-base-sentiment` or `nlptown/bert-base-multilingual-uncased-sentiment`; final choice by signals-alt after a quick sanity check) for the other languages. CPU batch 64 with `torch.set_num_threads(12)`, scoring only unscored items, about 50–100 items/s. Lazy Prices: TF-IDF cosine and Jaccard on the 10-K Item 1A and 7 sections and the 10-Q MD&A, plus Loughran–McDonald word deltas.
* **GDELT** (ingest-market / signals-alt): Events export is filtered by size and Goldstein, FIPS codes become ISO2, CAMEO root codes map to categories and sectors. GKG organizations are matched to issuer `short_name`/aliases with exact normalized matching (Polars `str.contains_any`, Aho-Corasick) and give daily per-issuer attention and tone.
* **Macro** (ml): a 4-state Gaussian HMM (hmmlearn) on monthly standardized core series (growth momentum, inflation momentum, 2s10s, HY OAS / BAA spread, VIX, broad USD). Weekly refit; nightly filtered probabilities (no smoothing, PIT-safe). States are labelled by their means. Per-stock 36-month rolling betas use Newey–West, plus per-regime mean excess returns. The macro factor = regime-probability-weighted expected excess return.
* **Technicals** (technicals): levels are swing pivots (k = 3, 5, 10 on daily bars, k = 2 on weekly); prior D/W/M/Q/Y open, high, low and close; floor and Camarilla pivots from the prior W/M; Fibonacci levels of the last major swing; anchored VWAPs (YTD, QTD, swing high/low, last earnings, 52-week high/low); 52-week and all-time extremes; round numbers; a 1-year volume-profile POC proxy. A weekly per-ticker event study (first touch → reaction ≥1 ATR before a 0.5 ATR adverse close, 10 bars) gives hit rate, expectancy and rank per `level_key`, shrunk toward the universe prior. Confluence: weighted Gaussian KDE (bandwidth 0.35·ATR14), peaks above the 60th percentile density become zones, which are ranked by strength. Each zone carries entry, invalidation, target and R:R.
* **ML** (ml):
  * Cross-sectional rank labels (1M/3M/6M forward excess vs sector×region).
  * Features: all factor metrics plus raw ratios, momentum, volatility, liquidity, insider, sentiment, macro betas, technical distances to zones and size, cross-sectionally rank-gauss normalized per date.
  * Rebalance dates: Fridays and month-ends. Training uses month-ends to reduce autocorrelation; inference runs daily.
  * Walk-forward: expanding window, minimum 5 years of training, 12-month test blocks. **Purge** training rows whose `label_end_date` ≥ test start. **Embargo** 10 trading days after each test block.
  * Models: XGBoost (`reg:squarederror` on rank labels, also `rank:pairwise` tested; `nthread=16`) with TreeSHAP. An FT-Transformer (d = 64, 3 blocks, 4 heads; per-feature tokenization) and a cross-sectional attention variant (attention across the stocks of one date, ≤ 1000 per batch). Both use CPU and torch threads = 16.
  * Ensemble: rank average weighted by trailing 24-month rank IC.
  * Evaluation: IC and rank IC per date, Newey–West t, IR, deciles (equal weight, 10 bp per side costs), long–short Sharpe, turnover, per-sector IC.
  * A model is promoted to production only if OOS rank IC t > 2 and it beats the incumbent. Promotion is manual or through the API.

## 12. Serving layer & performance budget

**Budget:** API p95 < 300 ms per call at MVP and at full scale. Company 360 first paint < 1.5 s (above-the-fold data in a single call).

**Serving snapshot contents** (built by `app.api.serving`, owner api). All tables are sorted by their lookup key so DuckDB zonemaps prune. There are no Parquet scans at request time.

| Serving table | Content | Serves |
|---|---|---|
| `serve_security` | One row per security: identity, sector, latest quote/returns, valuation, composite for all profiles, 10 factor scores, moat summary, ML summary, insider signal, sentiment score, top zone, dq score, badges | search, screener, overview, watchlists, sector leaders |
| `serve_prices` | Adjusted + unadjusted OHLCV, last 15 years, sorted (security_id, date) | price charts |
| `serve_financials` | Latest version per period (LOCAL + USD) in long format, plus PIT history for 10 years | financials tab, `as_of` views |
| `serve_metrics` | ratios + sector_kpis, last 10 FY + 12 TTM | ratios tab, peers |
| `serve_peer_scores`, `serve_score_history` | latest metric scores; factor/composite history 3 years | scores tab |
| `serve_sector_stats` | Precomputed KPI distributions and factor medians per sector × subsector × peer region × size | Sector Explorer |
| `serve_market` | Index/sector performance, breadth, movers | Market page |
| Others | Copies of forecasts/forecast_valuation (latest as_of), moat (latest + history), insider_transactions (5 y), insider_signals (1 y), news_items (180 d), sentiment_daily (2 y), filing_text_changes, gdelt_events (90 d), gdelt_company_daily (1 y), tech_levels, confluence_zones, level_type_stats, ml_predictions (live + 2 y), model_runs, model_eval_series, macro_series, macro_observations (latest vintage), macro_regimes, macro_betas (latest), filings_index (5 y), job_runs (90 d), dq_results (last run) | respective pages |

**API techniques**
* One shared read-only DuckDB connection with a cursor per request.
* `ORJSONResponse`, GZip ≥ 2 KB.
* Default payload caps: prices default 5 years daily; longer ranges are auto-resampled weekly.
* An in-process TTL cache (10 min, keyed by serving_run_id + path + query) for heavy aggregates (sectors, market overview, insider clusters, event map).
* Screener custom weights: `SUM(w_f * f_score) / SUM(w_f)` over `serve_security`. At ~20k rows this takes < 50 ms.

**Front-end contract**
* Company 360 calls `/overview` first, plus `/prices` in parallel. Tabs lazy-load.
* Every list endpoint is paginated (limit ≤ 500).
* The perf test (`backend/tests/perf_api.py`, api agent) reports p50/p95 per endpoint against a populated snapshot.

## 13. Data quality scoring

**Item level** (`fundamentals_canonical.dq_score`). Start at 1.0, then subtract the applicable penalties and clip to [0, 1]:

| Condition | Penalty |
|---|---|
| fallback tag | −0.10 |
| derived formula | −0.15 |
| derived period (e.g. Q4 = FY − 9M) | −0.05 |
| estimated knowledge date | −0.20 |
| item-specific note penalties (CN/BR quirks) | per note |
| failed accounting identity | −0.30 |
| cross-source disagreement > 2% | −0.10 |

**Row level** (`fundamentals_wide.dq_score`): the mean over the core items. `coverage` is the share of core items present.

**Security level** (`composite_scores.dq_score`): the mean of its inputs. The UI shows a warning badge below 0.6.

**Table level** (`dq_results`, job `dq_checks`):

| Check | Severity |
|---|---|
| schema conformance | error |
| PK uniqueness | error |
| null rate vs 30-day baseline (>2×) | warn |
| freshness (lag beyond expected for that table) | warn/error |
| row-count delta (±50% vs previous run) | warn |
| value ranges (e.g. \|daily return\| > 80% without a corporate action) | warn |
| prices cross-source agreement (Polygon vs yfinance close > 1%) | warn |
| PIT violations (knowledge_date < period_end) | error |
| FX coverage (currency without a rate in the window) | error |
| unresolved identities share | warn |

An `error` blocks `build_serving_db`, and the previous snapshot stays live. A `warn` is surfaced on the System page.

## 14. Error handling & retries

* **HTTP:** exponential backoff with jitter on transport errors, 408/425/429/5xx, up to 5 retries. Retry-After is honoured. 429 drains the bucket. 4xx other than 429 is never retried.
* **Adapter:** each entity is processed in its own try/except and recorded in `FetchResult.entities_failed`. **Circuit breaker:** if more than 50% of the first 20 entities fail with the same error class, the job aborts as `failed`. This avoids hammering a blocked endpoint (403, schema change).
* **Job:** status is `success`, `partial` (some entities failed; watermark advanced only for OK entities via per-entity cursors) or `failed`. The scheduler retries a failed job twice, 60 s and then 120 s apart. Downstream jobs run on `partial` and are skipped on `failed`, and the DAG continues on independent branches.
* **Snapshot safety:** if any upstream of `build_serving_db` failed, or a dq error exists, the snapshot is **not** published. The API keeps serving the last good one and `/health` reports `degraded`.
* **Idempotency:** every job can be re-run for the same as-of date. Writes are upserts on the PK.
* **Partial writes:** partition-atomic swaps mean a crash mid-write leaves the old partition intact. `.tmp-*` and `.old-*` leftovers are cleaned at worker start.

## 15. Logging & observability

* Standard `logging` through `app.core.logsetup.setup_logging()`. Text format `time level logger | message`. A `SecretRedactingFilter` on every handler masks literal key values and `api_key=…`-style params.
* Files:
  * `data/logs/api.log` and `data/logs/worker.log` (rotating 20 MB × 5).
  * `data/logs/jobs/<job_id>/<run_id>.log` (one per run; path stored in `job_runs.log_path`).
* Metrics per run in `job_runs`: duration, rows written, entities OK/failed, HTTP requests, cache hits. The System page (`/api/v1/system/*`) shows DAG status, freshness per table, dq failures and key presence (booleans only).
* Polars/DuckDB timings: wrap heavy steps with `app.core.logsetup.get_logger(...).info("step x took %.1fs")`. No external tracing.

## 16. Testing strategy

| Layer | What | Where / how |
|---|---|---|
| Contracts | Cross-file consistency (already 62 tests) | `backend/tests/test_contracts.py` — must stay green |
| Core | Config, schema conform/validate, storage roundtrip/upsert, DuckDB views, rate limiter, redaction | `backend/tests/test_core.py` |
| Adapters | Parse **recorded fixtures** (`backend/tests/fixtures/<source_id>/…`, small real payloads with keys stripped) → `normalize()` output passes `conform()`. Live calls are marked `@pytest.mark.network` and excluded by default. | per-agent `tests/test_ingest_<source>.py` |
| Canonical mapping golden tests | AAPL, MSFT, JPM (bank), Toyota (EDINET), Samsung (DART), ASML (ESEF/20-F), Petrobras (CVM), Kweichow Moutai (CAS): revenue, net income, total assets, CFO within ±0.5% of reported | `tests/test_fundamentals_golden.py` |
| PIT leakage | Append future-dated rows, then recompute features as-of D: outputs must be byte-identical. Labels must not overlap the training window after purge. | `tests/test_pit.py` (ref-identity + ml) |
| Analytics | Unit tests on synthetic frames (percentiles, fallbacks, KDE zones, backtest events, DCF identities: EV = Σ discounted FCFF + TV) | per module |
| API | `TestClient` against a tiny synthetic serving snapshot (fixture builder `tests/fixtures/serving_small.py`, api agent). Responses are validated against `contracts/api.yaml` required fields and types. Perf script for p95. | `tests/test_api_*.py`, `tests/perf_api.py` |
| End-to-end (Phase 3) | Nightly DAG on 20 tickers with fixtures + network smoke on 5 live tickers | `tests/test_e2e.py -m network` |

Run with `..\.venv\Scripts\python.exe -m pytest -m "not network"` from `backend/`.

## 17. Security

* Keys exist only in `.env` (git-ignored) and are loaded as `SecretStr`. `Settings.secret(name)` returns the plain value only to build a request. Keys are never written to logs, cache metadata, raw files, `job_runs` or API responses.
* HTTP cache keys exclude secret params. Cached URLs are redacted.
* The API binds to 127.0.0.1 only. CORS allows the Vite origins only. There is no auth (single-user, local).
* Pre-commit suggestion for Phase 3: a grep hook that blocks `.env` and any string matching the configured key values.

## 18. MVP slice & scale-up path

**MVP universe** (`universe_membership`, built by ref-identity)
* `mvp_us`: the ~500 largest US-domiciled common stocks by market cap (SEC CIK + Polygon grouped-daily close × dei shares). Excludes ADRs, SPACs, funds and REIT preferreds.
* `mvp_global`: ~200 names. EU 60 (EURO STOXX 50 + SMI/Nordic majors), UK 25 (FTSE top), JP 40 (TOPIX Core30 + large), KR 15, CN/HK 35 (A-share majors + HK megacaps), LATAM 20 (BR 12, MX 5, CL/PE/CO 3). The curated seed is `data/reference/seeds/mvp_global.csv` (`ticker, mic, yf_ticker, isin, name`).
* History: prices since 2000 where available. Fundamentals: 10–15 years US (XBRL since 2009), 5–10 years elsewhere.
* Data volume at MVP: about 3–5 GB including raw.

**Scale-up path**

| Stage | Universe | Main changes |
|---|---|---|
| S1 (MVP) | ~700 | as designed |
| S2 | US ~3,000 (mcap > $300M, price > $5) + global ~2,000 | yfinance batch scheduling spread across the day. GKG matching via Aho-Corasick. Serving prices trimmed to 15 years. Transformer trained on month-ends only. |
| S3 | all parseable (~15–20k) | Per-region nightly sub-DAGs staggered by market close. `fundamentals_raw` partitioned by `entity_bucket` (hash mod 16) as well. Optional Postgres for app_state if multi-user. Polygon paid tier if available. |

Contracts already carry the partition keys needed for S2/S3. Moving from S1 to S2 needs no schema change.

## 19. Module ownership & build phases

**Rule:** one writer per table, exactly as in each contract's `owner`. Agents communicate only through tables and contracts. Changing a contract means:
* bump `version`;
* keep `test_contracts.py` green;
* append a line to `docs/CONTRACT_CHANGES.md`;
* additive changes (new nullable column, new item, new metric) are allowed freely. Renames and removals need orchestrator approval.

`app/core/*` is shared infrastructure. Edits there must be additive and backward compatible.

| Agent | Phase | Owns (code) | Writes (tables) |
|---|---|---|---|
| **platform** | 1 | `app/scheduler/{worker,runner,alerts}.py`, `app/cli.py`, `app/core/dq.py`; maintains `app/core/*` | job_runs, dq_results, alert_events |
| **ref-identity** | 1 (critical path, start first) | `app/normalize/{identity,identity_universe,identity_figi,taxonomy,fundamentals,fx,formula,pit}.py`, `data/reference/seeds/*` | issuers, securities_master, listings, identifier_map, universe_membership, fundamentals_canonical, fundamentals_wide; back-fills issuer_id in normalized tables |
| **ingest-us** | 1 | `app/ingestion/{sec_index,sec_xbrl,sec_form4,sec_text,sec_13f,finra_si}.py` | filings_index (sec_edgar), fundamentals_raw (sec_edgar), insider_transactions (US), institutional_holdings, short_interest |
| **ingest-global** | 1 | `app/ingestion/{esef,edinet,opendart,cvm,cn_akshare,companies_house,yf_fundamentals}.py` | filings_index / fundamentals_raw (own source_ids), insider_transactions (KR, EU PDMR) |
| **ingest-market** | 1 | `app/ingestion/{polygon_prices,yf_prices,corporate_actions,fx,fred,macro_global,gdelt_events,gdelt_gkg,news,backfill_prices}.py`, `app/normalize/prices.py` | prices_daily, corporate_actions, fx_rates, macro_series, macro_observations, gdelt_events, news_items (rows) |
| **frontend** | 0–3 | `frontend/` | — (consumes `contracts/api.yaml`) |
| **analytics-scoring** | 2 | `app/analytics/{ratios,sector_kpis,valuation,scoring,scoring_ml_blend}.py` | ratios, sector_kpis, valuation_daily, peer_scores, factor_scores, composite_scores |
| **analytics-valuation** | 2 | `app/analytics/{moat,forecast,cost_of_capital}.py` | moat, forecasts, forecast_valuation |
| **technicals** | 2 | `app/technicals/{levels,level_backtest,confluence}.py` | tech_levels, level_type_stats, confluence_zones |
| **ml** | 2 | `app/ml/{features,feature_registry.yaml,labels,train,models/*,ensemble,predict,evaluate,macro_regime,macro_betas}.py` | ml_features, ml_labels, ml_predictions, model_runs, model_eval_series, macro_regimes, macro_betas |
| **signals-alt** | 2 | `app/analytics/{insider,insider_models}.py`, `app/nlp/{sentiment,lazy_prices,gdelt_link,aggregate}.py` | insider_signals, sentiment_daily, filing_text_changes, gdelt_company_daily, news_items (sentiment columns, scheduled after ingest) |
| **api** | 2 | `app/api/{routers/*,deps.py,serving.py,app_state.py}`, `tests/fixtures/serving_small.py`, `tests/perf_api.py` | serving snapshots, watchlists, watchlist_items, alerts |
| **bug-hunter / integrator** | 3 | cross-cutting fixes, e2e tests | — |

**Phase plan**
* **Phase 1** (parallel): platform, ref-identity, ingest-us, ingest-global, ingest-market. ref-identity first publishes `app.normalize.identity.lookup(id_type, value)`, `app.normalize.pit.asof()` and `app.normalize.formula.evaluate()` stubs with stable signatures on day 1, so the others can code against them.
* **Phase 2** (parallel): analytics-scoring, analytics-valuation, technicals, ml, signals-alt, api. Technicals and api can start during late Phase 1, because they only need `prices_daily` plus a synthetic snapshot.
* **Phase 3:** wire frontend ↔ API, then e2e and bug-hunt.

Dependencies between Phase 2 agents:
* analytics-valuation needs `ratios`.
* scoring needs `moat`, `forecast_valuation`, `insider_signals`, `sentiment_daily` and `macro_betas`. Missing inputs fall back to `neutral_50`/null per scoring.yaml, so scoring can ship first.
* ml needs `factor_scores` and `tech_levels`.

## 20. Decisions (trade-offs)

1. **Parquet lake + immutable DuckDB serving snapshot** instead of one live DuckDB file. DuckDB allows one writer process, and Windows file locking makes in-place swaps fragile. Snapshots give consistent reads and the 300 ms budget. Cost: ~3 min rebuild per night and roughly 2× disk for served data.
2. **Separate API and worker processes**, job triggering via a file queue. No broker is needed, and heavy CPU never blocks requests.
3. **Default write mode `upsert`** (merge touched partitions on PK). `overwrite_partition` is opt-in only, because incremental jobs would otherwise wipe partitions. Partition sizes are kept small so merges stay cheap.
4. **Adapters key by source-native ids; identity is resolved centrally afterwards.** This avoids order-dependent duplicate issuer ids when SEC and a home regulator both describe the same company (e.g. Petrobras: CIK and CVM code).
5. **issuer_id is assigned once, never recomputed.** Merges go through `issuer_alias`. Deterministic hashing of a seed alone would break when better ids arrive later.
6. **Long-format canonical facts plus a wide derived pivot.** Long keeps PIT versions and lineage. Wide gives fast analytics.
7. **USD-on-USD valuation ratios.** This removes ADR and dual-listing currency mismatches. Local-currency financials are still shown in the UI.
8. **TTM-first scoring with an FY fallback.** It covers quarterly (US/JP/KR/CN) and semiannual (EU/UK/BR partial) reporters uniformly.
9. **Own 11/79 taxonomy** (more granular than the 60–70 first suggested). Banks, insurers, REITs, payments and Japanese trading houses get proper KPI sets, and the peer fallback chain absorbs thin groups. This is not GICS, for licensing reasons.
10. **Precedence `override > yfinance industry > SIC > national codes`.** SIC is stale for modern business models (MSFT = 7372 "prepackaged software"). National codes are too coarse.
11. **Altman Z'' (book-equity) as the default distress score.** It is price-independent and valid across regions. The original Z applies to manufacturers only.
12. **Labels lagged by one day** (close D+1 → D+1+h). This slightly understates achievable alpha but guarantees no after-close filing leakage.
13. **Polygon only for recent US EOD via grouped-daily.** The free tier (5/min, ~2 y) cannot serve per-ticker history. yfinance supplies depth and global coverage, and Polygon cross-checks it.
14. **Sector classification is current-only** (minor look-ahead). Historical GICS-like histories are not freely available.
15. **Filtered (not smoothed) HMM probabilities.** PIT-safe, at the cost of noisier regime calls.
16. **Contract-driven 501 stubs** in the API (auto-generated from `api.yaml`). The frontend can integrate against every path from day 1.
17. **No LLM features** (per scope). NLP is limited to supervised sentiment classifiers and lexical similarity.
18. **Python 3.13.** Every pinned package installed from wheels (torch 2.14 CPU, xgboost 3.4, shap 0.53, hmmlearn 0.3.3, transformers 4.57). No substitutions were needed.
19. **`fiscal_year` = calendar year of the fiscal year end.** This is deterministic across JP (March) and US retail (January) year ends.

## 21. Open questions

1. **Keys.** `.env` is currently empty (0 bytes). FRED and Polygon are needed for Phase 1 live runs. EDINET (free registration), OpenDART (free), Companies House (free) and OpenFIGI (free) keys are strongly recommended. `SEC_USER_AGENT` must contain a real contact e-mail.
2. **Polygon tier.** If the user upgrades, set `configure_limiter("polygon", ...)` and switch US history to Polygon.
3. **China data.** AKShare scraping is legally grey and fragile. Should CN fundamentals stay best-effort (yfinance fallback), or is a Tushare token acceptable?
4. **EU insider (PDMR) data.** It has no unified free API. Keep it best-effort per country (AMF, BaFin, CNMV, CONSOB …) or drop it for MVP? The current design treats it as optional (`coverage: partial`).
5. **Benchmark indices** for Market & Macro. Index levels come from yfinance (`^GSPC`, `^STOXX50E`, `^N225`, `^KS11`, `^HSI`, `000300.SS`, `^BVSP`). Licensing is fine for local use, but confirm the user wants these.
6. **Multilingual sentiment model.** The final pick (license and quality) is left to signals-alt and should be documented in their module docstring.
7. **Moat thresholds** (70/50, persistence 0.8/0.6) are judgment calls. Calibrate after the first full run against a hand-labelled sample of 30 names.
