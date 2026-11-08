# Price history (Yahoo, batched): plan (resumable)

**Goal.** Long daily OHLCV history from Yahoo Finance for most of `securities_master`, plus market instruments (FX pairs, commodity futures and indices, volatility indices), stored as compactly as possible without breaking downstream consumers.

**Resume protocol.** Tick `[x]` and add a one-line result per item. State lives in `data/state/price_history.json`, with the batch plan, inventories and spill files under `data/state/price_history/`. Long tasks run detached:

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\price_history_launch.ps1 -Name <n> -TaskArgs "<args>"`

Logs and PIDs go to `data/logs/price_history/`. Re-running the same command resumes the task. A launch is skipped while its PID is alive. For a graceful stop, create `data/state/price_history/STOP`.

CLI from `backend\`: `..\.venv\Scripts\python.exe -m app.ingestion.price_history {inventory|plan|backfill|flush|compact|footprint|status}`

## Design

- **Fetch** (`app.ingestion.price_history`). Multi-ticker `yf.download(period="max", auto_adjust=False, actions=True, group_by="ticker")` in batches of 100 (instruments: 80).
  - Yahoo's split-adjusted Close/Volume are converted back to raw prints with the splits from the same download (`yf_prices.long_to_staging`). Dividends and splits go to `corporate_actions_staging`, and Yahoo's Adj Close is kept for validation.
  - Each batch is saved to a spill file (Arrow IPC) and marked `fetched` in `plan.json`. Every 10 batches the spill files are appended to `prices_staging` and marked `flushed`, so at most one in-flight batch can be lost.
- **Pacing.** One token from the shared `yfinance` bucket per download, plus a 6 s base pause.
  - The pause grows x1.5 after a throttled batch, up to 90 s, and decays x0.9 after a good batch.
  - A batch with fewer than 10% of its symbols returned triggers a canary download (SPY 5d). If the canary fails, the batch is treated as throttled (yfinance swallows HTTP 429 into empty frames): 5/10/20/40-minute cool-down, then retry. If the canary succeeds, the batch really is empty and is recorded as `nodata`.
- **Priority in `prices_finalize`.** polygon > krx > baostock = twse = tpex = **b3_cotahist** > yfinance. B3 was raised from 4 to 2: official and exchange sources win, and Yahoo fills gaps and earlier history. Overlaps are reconciled (1% close tolerance → `source_disagreement`).
- **Instruments** (`app.ingestion.price_history_lists`) are appended to `BENCHMARKS` with synthetic ids `IDX:<yf>` / `ETF:<yf>`, so every equity filter keeps excluding them.
  - `ref_universe` / `identity_resolve` register them with security_type `fx` (new, securities_master v4), `future`, `index` or `etf`. They are never members of an equity universe.
  - FX pairs use MIC `XOFF`, exchange_country = the quote currency's country, and their own prices_daily region `FX`.
  - **FX policy:** Yahoo FX OHLC series are market instruments only (charts, Model Studio, technicals). All conversions (`close_usd`, fundamentals, macro) keep using the official `fx_rates` table (ECB / FRED / central banks).
- **Storage** (contracts prices_daily v2, prices_staging v3; `app.core.storage`):
  - OHLC and vwap (plus staging adj_close) are stored as float32. Readers widen them to float64 and round to 7 significant digits: values with ≤7 significant digits round-trip exactly, and the worst case is 6e-8 relative (limit 1e-6).
  - Volume and the adjustment factors stay float64.
  - `adj_close`, `close_usd` and `dollar_volume_usd` are derived on read (`close*adj_factor`, `close*usd_per_unit`, `close*usd_per_unit*volume`). The new stored column `usd_per_unit` holds the official fx_rates value.
  - Rows are sorted by (security_id | symbol, date). Parquet uses zstd level 9, string keys are dictionary-encoded, and each partition is compacted to one file.
  - Every reader goes through the contract: `scan_table`, `view_sql`, the serving lake views and exported Studio scripts.
  - Staging retention was **not** shortened, because `prices_finalize` rebuilds each region from the full staging history (source priority, adjustments, reconciliation).

## Progress

- [x] H0 Inventory (`inventory --tag before`)
  - 2026-10-13: 24,831 securities, 24,464 priced, 93.3 M bars, median 14.5 y; 16,510 already had Yahoo history.
  - Never fetched from Yahoo: CN 5,203, TW 1,980, BR 367, US delisted 753, KR/HK 13, BENCH 3.
  - Spikes in Yahoo first dates (JP 2000/2001, KR/HK/EU 2000-01) match Yahoo's own coverage limits, so those symbols needed no refetch.
- [x] H1 Instruments probe + registration
  - 125 of 138 candidates are served by Yahoo: 78 FX pairs, 18 futures, 2 commodity indices, 13 commodity ETFs, 14 volatility indices.
  - Not on Yahoo: USDBGN, LBS=F, UX=F, ^BCOM, ^BCOMTR, ^CRB, ^RJI, JJA, ^RVX, ^EVZ, ^V2TX, ^VHSI, ^JNIV (alternates tried; see `price_history_lists.UNAVAILABLE`).
- [x] H2 Plan: 86 batches. Instruments 128, `full` without Yahoo history 7,473 (CN unpriced tail first, then shortest history first), rest 845 (BR non-full and US delisted; delisted symbols re-used by an active listing are skipped).
- [x] H3 Backfill (`-Name backfill -TaskArgs "backfill"`) — 2026-10-13 22:17-22:36 UTC: 86/86 batches, 7,818 / 8,446
  symbols returned data (instruments 119/128, full_gap 7,456/7,473, rest 243/845 — US delisted mostly absent on Yahoo),
  28.2 M bars + 106 k corporate actions staged, 1,096 s fetch time (~7.7 symbols/s, ~25.7 k bars/s), 0 throttle events
  (canary never failed). 6 instruments turned out quote-only (no history) -> UNAVAILABLE.
- [x] H4 Storage — contracts + readers/writers + tests; `compact` 2026-10-13: prices_staging 3.047 GB -> 1.897 GB
  (23.47 -> 14.62 B/row, 1,779 -> 200 files, 129.8 M rows, 0 duplicate PKs), prices_daily 3.033 GB -> 1.286 GB
  (32.51 -> 13.78 B/row); total 6.08 -> 3.18 GB. Experiments (US 2024 + JP 2010 sample): zstd level 3->15 only -2%,
  row-group 256k -0.5%, byte-stream-split worse; dropping derived columns -51%; float32 OHLC a further -9% (daily) /
  -13% (staging); sort by (symbol, date) -20% on staging. Volume kept float64 (2-6% fractional, int64 no gain).
- [x] H5 Post-backfill chain (`scripts/price_history_chain.ps1`; log `data/logs/price_history/chain*.log`, done-markers
  `data/state/price_history/chain*_<step>.done`): ref_universe (refresh_yf=false; securities_master 24,831 -> 24,950,
  +119 instruments) -> prices_finalize all regions (chunked, 2,125 s) -> identity_resolve. build_serving_db hit DuckDB
  OOM in the `prices` snapshot table (per-(security, date) dedupe window over ~70 M rows) -> serving.py now builds it in
  4 ordered security_id ranges (identical result). Stitch guard added (`prices._stitch_guard`: Yahoo bars before the
  official series start are dropped when Yahoo disagrees at the junction; Yahoo gap-fills dropped when > 5% of
  overlapping days disagree) -> pass 2 (`-Tag chain2 -Regions APAC,CN,LATAM -Skip ref_universe,identity_resolve`):
  dropped 513 k TW / 61 k CN / 260 k BR Yahoo rows (786 / 194 / 129 securities).
- [x] H6 2026-10-14: serving snapshot `serving_R20261013T2347198377` published (prices built in 4 ranges, ~7.5 min).
  Inventory after (`compare`): priced 24,464 -> 24,932 securities, bars 93.3 M -> 98.3 M, 2,041 securities with longer
  history; CN priced 4,875 -> 5,224 (BaoStock tail filled from Yahoo, earliest 1990-12-19, +5.7 y median gain for 1,043
  names), TW 709 extended (+2.1 y), BR 87 extended (+8.7 y), US 83 extended (+4.3 y); instruments: FX 77 (median 22.9 y),
  futures 47, indices 59, ETFs 180. Storage after finalize: prices_daily 1.21 GB (12.3 B/row, 98.3 M rows),
  prices_staging 1.90 GB (14.6 B/row). Reconciliation official vs Yahoo raw close (share of overlapping days > 1%):
  polygon 0.47%, baostock 2.7%, twse 17.1%, tpex 16.6%, b3_cotahist 19.1% (TW/BR concentrated in early years: Yahoo
  back-adjusts for events it does not publish as splits; COTAHIST has pre-2005 quote-factor anomalies). DQ: instruments
  fresh 99-100%, history >= 20 y median; ohlc_consistency warns for fx (6.2%) and futures (4.4%) (Yahoo FX/futures
  OHLC quirk). Studio catalog: `instruments` (363) in /studio/catalog/datasets; FX/futures usable via EntityFilter tickers.

## Queued / follow-ups

- The API process (:8010) was started before these code changes: restart it to load the new storage readers (Model
  Studio lake views over prices_daily need them; the serving snapshot is unaffected).
- Technicals / macro exposures / ML pick the new history up in the nightly DAG (not re-run here).
- US: 35 securities where Yahoo history and Polygon disagree at the junction (not guarded; pre-existing behaviour).
- BR COTAHIST pre-2005 unit anomalies (e.g. VULC3 0.00045 vs 45) now win over Yahoo by policy: needs a B3 quote-factor /
  grupamento review in app.ingestion.b3_cotahist.
- Yahoo has no daily history for: ^V2TX (VSTOXX), ^BCOM, ^CRB, ^RVX, ^VHSI, ^JNIV, ^EVZ, USDBGN, USDCNH, UX=F, B0=F,
  MCL=F, ^DJCI, ^VIX1Y, ^VXEEM; 607 US delisted symbols returned nothing.
- Nightly `prices_global_yf` keeps CN/TW/BR on official sources only (OFFICIAL_BULK_SUFFIXES); the 349 CN names priced
  only from Yahoo stay frozen until BaoStock (blacklisted, see MODEL_STUDIO_PLAN P1) resumes.

## Instruments

- **FX** (`=X`, price = quote per 1 base; volume is usually 0):
  - EURUSD, GBPUSD, AUDUSD, NZDUSD
  - USD vs JPY CHF CAD SEK NOK DKK SGD HKD CNY CNH KRW TWD INR IDR THB MYR PHP VND PKR MXN BRL ARS CLP COP PEN ZAR TRY PLN CZK HUF RON ILS SAR AED QAR KWD EGP NGN KES KZT RUB ISK
  - EUR vs GBP JPY CHF SEK NOK DKK PLN CZK HUF RON TRY AUD CAD NZD CNY HKD SGD ZAR MXN INR BRL KRW ISK
  - Crosses: GBPJPY, GBPCHF, GBPAUD, AUDJPY, AUDNZD, AUDCAD, CADJPY, CHFJPY, NZDJPY
- **Commodity futures** (front month, `=F`):
  - Already covered by P2: CL BZ NG RB HO GC SI HG PL PA ZC ZW ZS KC SB CC CT LE HE
  - Added: ZO ZL ZM KE ZR OJ LBR GF DC ALI HRC TTF MGC QM B0 SIL MCL, ETH (CME)
- **Commodity indices / ETFs:**
  - Indices: ^SPGSCI, ^DJCI
  - ETFs: GSG DJP COMT BCI USCI DBB DBE DBO DBP UGA PALL SIVR SGOL (the P2 list already has GLD IAU SLV USO BNO UNG DBC DBA PDBC CPER PPLT URA WEAT CORN)
- **Volatility indices:**
  - Added: ^VXN ^VVIX ^VIX9D ^VIX3M ^VIX6M ^VIX1Y ^VXD ^OVX ^GVZ ^VXEEM ^SKEW ^MOVE ^INDIAVIX ^AXVI
  - ^VIX was already covered by P2
  - These are excluded from macro exposures as levels, not returns.
