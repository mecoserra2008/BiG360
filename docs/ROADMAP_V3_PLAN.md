# Roadmap v3: news engine, Studio presets / Model Lab, BQL-style functions (plan)

**Status:** plan. Waiting for the user's OK and the decisions in §5.
**Requested 2026-10-22.** Technicals Bundle C (analogs, regime detector, narrative) is still pending; §4 places it in the order.

## N. News engine: historical compaction, live terminal, hourly compaction

**Today**
- GDELT E6 backfill, compacted into stories and labels, newest first, now back to about 2024-09.
- A nightly `gdelt_stream`, plus Polygon, RSS and GDELT DOC through `news_ingest`, and `story_cluster`.
- The R1 realtime layer (an SQLite bus behind the live SSE stream) exists but is **not running** in app mode; the header shows "realtime layer stopped".
- Sentiment is aggregated daily.

### N1. Historical coverage
- **GDELT:** finish E6 back to 2020 through the gap filler, already running. Optionally extend to 2015 (GDELT 2.0 starts 2015-02).
- **CC-NEWS (Common Crawl news crawl, 2016 onwards):** an open dataset of global news articles in WARC files. We would store **derived data only**, never full text: URL, title, publisher, time, language, entities, issuer links, themes, sentiment, MinHash. These feed the same story layer with the same compaction. A batched, capped-disk backfill like E6 (raw deleted after each batch).
- **Official sources:** SEC 8-K and other filings as "corporate news" events back to 2001; already in the lake, just not merged into stories. Also central-bank and statistics-office press releases (RSS archives where they exist).
- **Polygon news history:** as far back as the current plan allows.
- **Compaction:** the existing compact story/label format, with a retention policy (full members kept 120 days, then compacted to story level) and a monthly re-cluster across sources.

#### N1 implementation steps (news-hist agent; state `data/state/roadmap_v3.json` key `news_hist`, details `data/state/ccnews.json`, `data/state/news_filings.json`)
- [x] **N1.1 CC-NEWS ingestion** (`app/events_v2/ccnews.py`, `ccnews_parse.py`, `hist_merge.py`; table `ccnews_articles`): WARCs streamed in memory (never on disk), only the HTML head is decompressed (the parser jumps to the next gzip member), derived metadata only (title, <= 300-char description, URL + hash, publisher, language, time, issuer links, country / sector / theme labels, story sentiment, MinHash band keys), "label, then forget" (kept iff theme or issuer). Free-text linker rules tuned on 2026-09 (spot check ~82% precision, 50 links). Common Crawl ToU row in `docs/it/DATA_LICENSING.md`.
- [x] **N1.2 SEC 8-K + exchanges as corporate news** (`app/events_v2/filing_news.py`, table `filing_news`, sources `sec_8k` / `exchange_ann`): 805,517 8-Ks 2001-01 -> 2026-10 + 4,888 TDnet + 1,889 HKEX, all issuer-linked, theme by item, item polarity (1.03 / 2.04 / 2.06 / 3.01 / 4.02 / 1.05 negative, else 0). 411k stories merged; 401k (2015-02 -> 2024-09) wait for their GDELT day and are merged by the nightly job as E6 advances. CVM / FCA NSM rows have no headline (skipped).
- [x] **N1.3 GDELT back to 2015-02-19:** `gdelt_e6` campaign window extended (coverage.yaml); continues newest-first below 2020 (661 / 4,163 days; ETA ~180 h at ~20 days/h).
- [x] **N1.4 Campaign wiring:** gap-filler campaigns `ccnews_history` (module, newest month first, 6 polite connections, `--yield-to gdelt_e6`, progress probe `ccnews_months`, est 2 GB, BelowNormal) and `filing_news_history` (one-time, done); jobs `news_filings` (nightly incremental + repair) and `news_recluster` (monthly) in jobs.yaml v37.
- [x] **N1.5 Compaction / retention:** same compact format as E6; CC-NEWS members only < 120 d; stories older than 180 d pruned with the E6 story rule; ccnews_articles salient-only after 120 d; GDELT-gated merge + repair after GDELT day re-writes; monthly cross-source re-cluster (GDELT / CC-NEWS / RSS-Polygon).
- [x] **N1.6 Pilot 2026-09 end to end:** 374 WARCs / 400 GB streamed, 7.49M titled pages, 416,701 labelled articles; after the final link rules 366,907 stored (~12.2k/day), 37% issuer-linked (spot check ~82% precision), overlap with GDELT 35% (17.5% same URL, 17.9% joined a GDELT story by MinHash), ~160 B per article in the 120-day form (+ ~50 B story rows) and ~78 B long-term; 136-224 WARCs/h (40-67 MB/s, download-bound; CPU ~13 MB/s per worker). Full 2016-08 -> now: 52,234 WARCs = ~56 TB streamed (the plan said 2-4 TB), ~45-50M labelled articles, ~4 GB on disk long-term, ~10-16 days of continuous download at 40-67 MB/s (`--file-step 4` = 25% uniform sample, ~3-4 days). The overnight pilot peaked at 3.8 GB RAM (sentiment model loaded next to the download pool): fixed by running the merge and each sentiment model in child processes after the pool is shut down (not yet re-measured end to end: the network dropped to ~19 KB/s on 2026-10-23).
- [ ] **N1.7 Campaign running through the gap filler** (after the filler restart; yields to E6 while E6 waits for the filler).
- [ ] Central-bank / statistics-office press-release archives and Polygon news history: not started (out of this task's scope).

### N2. Live feed (< 60 s target from publish to screen)
- **Sources:**
  - GDELT 15-minute files, polled every minute and picked up seconds after release;
  - publisher RSS/Atom feeds polled every 1–2 min with ETag / If-Modified-Since: Reuters, AP, FT, WSJ, CNBC, MarketWatch, Bloomberg headlines where RSS exists, Nikkei Asia, SCMP, Les Échos, Handelsblatt, Expansión, Valor, central banks, regulators, exchanges' company-announcement feeds;
  - Polygon news (plan permitting);
  - Hacker-News-style aggregators only where they offer an API or RSS.
- **Pipeline:**
  - fetch → normalise → dedupe (URL hash + MinHash) → entity/issuer linking (existing) → theme/sector/country labels → FinBERT/XLM-R sentiment (cached, batched) → live story attach;
  - publish on the R1 bus, then the SSE stream to the UI;
  - target end-to-end p95 under 60 s for RSS and under 3 min for GDELT.
- **Supervisor:** runs the realtime layer as a 4th child (live news plus the macro release watcher), governed for RAM, with a toggle in the tray.

### N3. Hourly compaction
An hourly job folds the hot live store into the compact lake partitions:
- stories and members;
- labels;
- `event_daily_agg`, made **hourly** (`event_hourly_agg`);
- `sentiment_hourly` per issuer, sector, country and theme.

A daily finalise step then re-clusters across the day, applies retention, and refreshes serving deltas. Serving gets a small "intraday news" table refreshed hourly **without** a full snapshot rebuild (attached as a second DuckDB, or hot-swapped).

### N4. News Terminal page (keyboard-first, fast)
- **List:** a virtualised list (thousands of rows), auto-updating over SSE, with a flash/unread state, story grouping with source count, and sentiment/salience colouring.
- **Filters:** ticker, watchlist, country, sector, theme and source tier.
- **Keyboard:** `j/k` navigate, `/` filter, `w` watchlist only, `o` open, `s` mute story.
- **Detail pane:**
  - the story timeline, with the price reaction since publication for linked tickers;
  - related filings and events;
  - an "impact" estimate from the event-study library, labelled model-based.
- **Alerts:** a watchlist-news alert (tray toast plus sound option) and a "story velocity" alert (a sudden burst of articles).
- **Performance budget:** first paint under 1 s and updates in under 100 ms. The QA smoke suite gets a latency check.

### N2–N4 implementation steps (news-live agent; state `data/state/roadmap_v3.json` key `news_live`)
- [x] N2.1 Curated feed list `config/news_feeds.yaml` (official, regulators, exchanges, publishers), each verified (HTTP, items, robots, terms / RSS-intended); rejected feeds kept with the reason.
- [x] N2.2 GDELT live: lastupdate (+ translation) every 60 s, all six 15-min files parsed with the events_v2 parsers in a recycled child process into the GDELT hot slot store (`data/realtime/gdelt_hot/`).
- [x] N2.3 RSS/Atom poller from the config: ETag / If-Modified-Since, per-host politeness and back-off; headline + snippet ≤ 300 chars only.
- [x] N2.4 News desk consumer: URL hash + MinHash story attach, headline issuer linking (events_v2 alias table), country / sector / theme labels, FinBERT (shared, loaded once) → `news.item` / `news.story` on the R1 bus; hot items / assignments per hour (`data/realtime/news_hot/`).
- [x] N2.5 Latency metrics publish → bus (p50 / p95 per source), items / hour, last poll, failures (status file + `/metrics`).
- [x] N2.6 Supervisor 4th child `realtime` (RAM cap 1.5 GB, restart on crash), tray toggle "Live news on/off", header strip LIVE.
- [x] N2.7 No double downloads: `Streamer.fetch_day` reads hot slots; finalised days are checkpointed in `events_v2.json` (label `live`).
- [x] N3.1 Hourly fold (`hourly` DAG): hot store → partial-day lake files (stories, members, event / article labels) + `event_hourly_agg` + `sentiment_hourly` (XLM-R for non-English in the job).
- [x] N3.2 Daily finalise: full-day re-cluster from hot slots, live news merged, roll-ups (`event_daily_agg`, `sentiment_label_daily`), retention, checkpoints.
- [x] N3.3 Intraday DuckDB (`data/db/intraday_*.duckdb`, pointer `INTRADAY`, 7 days) hot-swapped by the API; reads merge snapshot + intraday + bus.
- [x] N3.4 jobs.yaml / schedule.yaml (`hourly` entry, all profiles) / coverage.yaml; Data Health "Live news" panel.
- [x] N4.1 API `/news/*` (terminal, stories, hourly, live health, feeds) + SSE topics `news.item` / `news.story`.
- [x] N4.2 News Terminal page `/news` (+ `/news/top`): virtualised list, live SSE, filters, keyboard, detail pane.
- [x] N4.3 Alerts `watchlist_news` / `story_velocity` (live consumer, tray toast, optional UI sound).
- [x] N4.4 QA smoke latency check for `/news` (first paint < 1 s, live update render < 100 ms).

**Licensing note:** for personal research use, RSS feeds are designed for consumption, and GDELT and Common Crawl are open. Scraping article pages that forbid it (robots.txt / ToS), or storing full text, is avoided. Bank use needs licensed news (Dow Jones/Factiva, LSEG, Bloomberg), as already noted in `docs/it/DATA_LICENSING.md`.

## S. Model Studio presets and Model Lab redesign

**Model Studio presets: from about 22 generic ones to a curated library of about 40 "research recipes" grouped by question.** Each preset:
- pre-fills dataset, method and parameters;
- explains *why* in two lines;
- states the validation status of the method on our data;
- links its result to Backtest / Thesis.

| Question | Recipes (examples) |
|---|---|
| **Macro → markets** | Rate-sensitivity of a sector (OLS + Newey-West on Δ10Y, ΔBEI, ΔDXY); recession probability (probit on curve + credit + claims); inflation surprise → equity sector event study; regime-conditional sector returns (Markov switching) |
| **Factors & cross-section** | Fama-MacBeth on our factor panel; IC decay of the evidence-based technical factors; quintile portfolios with costs; size/region interaction |
| **Events** | Earnings PEAD event study; insider-cluster CAR; CFTC extreme → futures forward returns; geopolitical shock (GPR) → sector IRF (local projections); news-sentiment shock → next-day returns (panel) |
| **Risk & co-movement** | DCC correlations of a basket; conformal peer-group ellipsoid; PCA of the yield curve; connectedness (Diebold–Yilmaz) of sectors / countries |
| **Forecasting** | SARIMA/ETS on a macro series with conformal intervals; ARIMAX with leading indicators; nowcast vs official release |
| **Fundamentals** | Valuation regression (EV/EBIT on growth, ROIC, risk); margin mean-reversion panel; DiD around a policy event |
| **Spatial / network** | Spatial lag of country shocks; supply-chain shock propagation |

- Every preset runs end-to-end in the QA suite.
- A "recipe card" UI with search.
- "Save as my preset".

**Model Lab: replace the confusing ML leaderboard with a "Model Registry & Monitoring" hub.** One place for every model that feeds the app:
- XGBoost forecasts;
- conformal envelopes and peers;
- techprob T2;
- meta-labelling;
- regime HMMs;
- macro expectations / nowcasts;
- earnings expectations.

For each model:
- what it does, its inputs, and its training window;
- its **validation gate** (pass/fail, ECE/coverage/IC with links to the validation docs);
- **live monitoring:** rolling calibration and coverage, drift alerts, last refit, data freshness;
- version history, and a comparison with the previous version;
- "used by" (which pages and alerts).

The leaderboard becomes one tab inside it. This merges the governance information that is currently scattered across validation docs and Data Health.

## B. BQL-style query engine + Bloomberg-like functions

**B1. Query language (`BQL-lite`).** A small declarative language compiled to point-in-time DuckDB SQL over the serving snapshot and the lake. Examples:
```
get(px_last, chg_pct(px, 1M), pe_fwd, roic, tech.hi52_pct)
  for(members('S&P 500') & sector('Tech'))
  with(dates=range(-2Y, 0D), freq=W, currency=USD, fill=prev)
  where(roic > 15% and tech.ivol_pct < 30)
  sort(chg_pct desc) limit(50)
```
- **Universes:** members(index/watchlist/screen/peer group), region(), sector().
- **Fields:** a **field dictionary** auto-generated from the contracts: every serving table and column gets a mnemonic, unit, description and PIT rule.
- **Time-series functions:** chg, chg_pct, avg, std, zscore, rank, pct_rank, rolling, lag, resample, ytd.
- **Groupings:** group(sector).agg(median).
- **Point-in-time:** `as_of=`.
- **Engine:** a parser (lark/handwritten), a safe compile to parameterised SQL (no raw SQL injection), cost limits, and caching.
- **Endpoints:** `POST /bql`, plus `GET /bql/fields`. These are also usable from Model Studio as a dataset source, from Backtest as a signal formula, from the C# client and from Excel (CSV/JSON).

**B2. Bloomberg-like mnemonic functions in the command bar (Ctrl+K).** Type `AAPL US <Equity> GP` or just `AAPL GP`:

| Function | What it opens |
|---|---|
| DES | description |
| GP / GIP | price chart / intraday-like |
| FA | financial analysis |
| RV | relative value vs peers |
| EE | earnings estimates (model-based) |
| ERN | earnings history |
| DVD | dividends |
| HDS | holders |
| SI | short interest |
| CN | company news |
| TECH | technicals |
| OMON | n/a: no options data |
| WEI | world equity indices |
| WIRP | implied policy rates |
| ECO | economic calendar |
| ECST | macro stats |
| GC | curves |
| FXIP | FX |
| CMDTY | commodities |
| COT | CFTC |
| EQS | screener |
| PORT | backtest/portfolio |
| BQL | query console |
| TOP | top news |
| N | news terminal |
| ALRT | alerts |
| THES | thesis |

- Each mnemonic routes to the right page with parameters and has `HELP` text.
- Unknown input falls back to search.

### B1–B2 implementation steps (bql agent; state `data/state/roadmap_v3.json` key `bql`)
- [x] B1.1 Field dictionary `app/bql/fields.py` → `contracts/bql_fields.yaml` (generated from the contracts + serving schema + curated aliases: 817 fields, 146 aliases, 616 with point-in-time history); test that every mnemonic / alias resolves and runs.
- [x] B1.2 Grammar `app/bql/grammar.py`: hand-written lexer + recursive-descent parser (lark not installed), canonical text, normalised AST hash; golden-AST tests.
- [x] B1.3 Compiler `app/bql/compiler.py`: parameterised DuckDB SQL over the snapshot (daily panel for time-series functions, grid sampling, cross-sectional layers), universes (members / region / sector / country / ticker lists, & | -), PIT `as_of` on knowledge dates, currency via close_usd / fx (lake FX history), weekly bars before the 15-year window, lake history for valuation / tech factors / conformal.
- [x] B1.4 Engine `app/bql/engine.py`: limits (universe, cells, daily bars, rows), timeout (DuckDB interrupt), result cache (snapshot + AST hash + universe), exports CSV / XLSX / JSON, PIT panel helper.
- [x] B1.5 API (api.yaml 1.37.0): POST /bql, /bql/explain, /bql/export; GET /bql/fields, /bql/resolve; saved queries /bql/saved (app_state `bql_saved`).
- [x] B1.6 Integrations: Model Studio source `bql`, Backtest Lab signal source `bql` (PIT per rebalance), Screener "Open as BQL".
- [x] B1.7 BQL console `/bql`: highlighted editor + autocomplete, results grid + chart, explain, exports, saved, history, examples.
- [x] B2.1 Command bar mnemonics (`frontend/src/lib/mnemonics.ts`): `<TICKER> [<MKT>] [<yellow key>] <MNEMONIC>`, plain mnemonics with arguments, `BQL get(...)`, `HELP <MNEMONIC>`, suggestions after ticker + space, recent commands; News Terminal fallback when `/news` is absent.
- [x] B2.2 USER_GUIDE §8 with the full mnemonic table.
- [x] B1/B2 headless Chrome check on a test API (copied snapshot, both themes, zero console errors).
- [ ] Go live: `npm run build:app`, restart the API (tray) — no snapshot rebuild needed.

**B3. "Called automatically for analysis".**
- **Saved BQL queries and monitors** that run on the scheduler (e.g. "every Saturday: tech names with hi52 > 95% and squeeze"). Results are stored and diffed, and alerts fire on new entries.
- **Function templates in pages:** every panel's "Copy as BQL" button shows the query behind it, so anything on screen can be re-run, modified or exported.
- **Deterministic "analysis packs" (no LLM):** for a ticker, run DES + FA + RV + TECH + CN + earnings + smart money and produce a one-page tear sheet (PDF/Markdown) on demand or on schedule for a watchlist.

## 4. Proposed order
1. **N2 + N3 + N4:** live news, hourly compaction, News Terminal. This is the fastest visible value, and it also turns the realtime layer on.
2. **B1 + B2:** BQL-lite and the mnemonic command bar, the foundation for "automatic" analysis.
3. **S:** Model Registry & Monitoring, plus the curated Studio recipes, which use BQL datasets.
4. **B3:** monitors, tear-sheet packs, "Copy as BQL".
5. **N1:** historical CC-NEWS / 8-K / GDELT 2015 compaction. Long-running in the background via the gap filler; it can start any time.
6. **Technicals Bundle C:** analogs, regime detector, narrative. It can slot in after 2 or 3.

## 5. Decisions for the user (defaults recommended)
1. **Live sources:** start with GDELT 15-minute + official RSS (central banks, regulators, exchanges) + major publisher RSS headlines + Polygon news. Article-page scraping is avoided. *(recommended)*
2. **CC-NEWS historical backfill:** yes, derived data only, 2016 onwards, disk-capped like E6. About 2–4 TB download over weeks on unlimited data; resumable.
3. **Hourly compaction** for news, events and sentiment; nightly stays for everything else.
4. **BQL syntax:** Bloomberg-like `get / for / with` as above.
5. **Model Lab** becomes the Model Registry & Monitoring hub, with the leaderboard as one tab.
6. **Order:** as in §4, with Bundle C after BQL.
