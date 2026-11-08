# EU (and European) universe expansion: resumable plan

**Goal:** widen European coverage from 90 curated names to roughly the whole investable listed universe. That means about 1,500 names: EU-27, plus UK, CH and NO via the same machinery where applicable. Every fact must come from an official dated source (ESEF via filings.xbrl.org, or a national filing archive), with yfinance only as fallback.

**Resume protocol.** This applies to both API rate limits and Claude session limits.
- Work the steps in order. When a step is done, tick it here (`[x]`) and add one line of result (counts, run ids).
- Long downloads run as **detached background processes** with persisted cursors under `data/state/`. They keep running if the agent stops, and re-running them resumes. Log progress to `data/logs/eu_expansion/*.log`.
- Before starting any step, read this file and `data/state/eu_expansion.json`, which holds machine-readable progress.
- Never restart a finished step. Never delete landed raw data.
- **Unattended chain (added 2026-10-11 20:14):** `scripts\eu_expansion_chain.ps1` runs steps 2→6 in sequence, skipping finished steps and backing off 15/30/45 min when a step fails (Yahoo 429s). Its PID is in `data\logs\eu_expansion\chain.pid` and its log in `chain.log`. If it is not running and steps remain, relaunch it detached: `Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","<root>\scripts\eu_expansion_chain.ps1" -WindowStyle Hidden`. Step 7 (report) stays manual.

## Steps
- [x] 1. **Filer census.** List every ESEF filer on filings.xbrl.org: LEI, country, latest period, number of filings. Then run every national archive's discover step for its full market, not just the universe: DILA FR, AFM NL, FSMA BE, eMarket/1Info IT, Nasdaq Nordic, Oslo NewsWeb, CNMV ES, FCA NSM UK. Write the census to `data/reference/eu_census/filers.parquet`.
- [ ] 2. **Identity mapping.** LEI → ISIN(s) via the GLEIF ISIN–LEI relationship file (bulk daily file, no API calls) and ESMA FIRDS (weekly full file: ISIN, MIC, CFI equity shares). Then ISIN → ticker and exchange via OpenFIGI. Use the key if `OPENFIGI_APIKEY` is set; otherwise 25 requests/min × 10 ISINs, resumable, cached. Then map to a yfinance symbol (exchange suffixes) and verify that prices exist. Keep only ordinary or common shares (CFI `ES*`) on a primary EEA/UK/CH/NO venue.
- [ ] 3. **Universe.** Rank by market cap from yfinance (price × shares). Create `eu_broad` = all of these with market cap ≥ €300M and liquid prices. Add them to `full`. Keep `mvp_global` unchanged. Write universe_membership additively, via seeds/registry, with no new issuer_id assigned twice.
- [ ] 4. **Prices.** Run backfill_prices (yfinance max history, `polygon_days=0`) for the new symbols in batches, resumable.
- [ ] 5. **Fundamentals backfill.** Run the per-LEI ESEF sweep plus `oam_filings_history` for all eu_broad issuers, as detached resumable runs. Arelle parses go through `_arelle.py`, which already skips converted filings. Then run yf_fundamentals for eu_broad names with no official source.
- [ ] 6. **Normalize and analytics** for the full universe: identity_resolve → fundamentals_normalize (`--universe full`, `full=true`) → ratios_kpis → valuation_daily → scoring (history + nightly) → moat → forecast → tech_levels → confluence_zones → build_serving_db.
- [x] 7. **Report.** (2026-10-12; see the Progress log) Coverage by country: names, official latest FY, history depth. Add a CHANGE note to `CONTRACT_CHANGES.md` and an update to `docs/USER_GUIDE.md` §4 (universes).

## Progress log
(append one line per completed step)
- Step 1 (2026-10-11, 541 s): 25,825 ESEF filings / 7,124 LEIs (3,912 European, 1,506 with period >= 2025-06-30); FIRDS 7,852 European ES* share ISINs / 7,627 issuer LEIs; GLEIF ISIN-LEI 6.8M European rows; DILA 391 LEIs, AFM 542 issuers -> `data/reference/eu_census/filers.parquet` (8,860 European rows). eMarket/Nordic/Oslo/CNMV/FSMA have no market-wide listing: queried per issuer in step 5; FCA NSM market-wide census (`fca_census`) hit 403 (WAF) -> UK covered per LEI in step 5.
- Steps 2–6 done via scripts/eu_expansion_chain.ps1 (finished 2026-10-12 09:23). build_serving_db first timed out at 60 min (contention); it ran standalone in 263 s. The DQ gate then blocked on 457,755 duplicate keys in prices_staging (concurrent appends during the price-coverage work); deduped and published (SV snapshot, 96M rows, 436 s).
- Step 7 (2026-10-12): eu_broad = 1,708 issuers. Official FY≥2025 for 927 (54%); any source 1,671 (98%); yfinance-only 744.
  By country (names / official FY25 / median official FY-years): GB 336/266/6, SE 199/113/5, DE 170/2/0, FR 161/151/6, CH 146/4/0, IT 106/65/4, NO 78/66/6, ES 72/8/4, NL 66/63/6, PL 60/0/3, FI 59/54/7, BE 52/48/6, DK 48/40/6, GR 33/0/3, AT 32/30/7, LU 15/0/3, PT 14/0/2, RO 14/0/1.5, IS 12/6/5, IE 11/7/6, others under 6.
  Gaps / next: DE and CH (no free official source); ES official FY2025 only 8 (CNMV FY2025 deposits not yet parsed for eu_broad?); PL/GR/PT/LU/RO/HU/CZ have no national adapter and filings.xbrl.org lags. Candidates: Warsaw ESPI/GPW, Athens ATHEX, Lisbon CMVM (no API).
