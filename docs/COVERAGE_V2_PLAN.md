# Coverage v2: earnings for all, regulator-complete US/EU/UK, rates & debt (plan)

**Requested 2026-10-31.** Goals:
- Earnings coverage for every security, not only the MVP universes.
- US, EU and UK coverage that is complete against each **securities regulator's** register, with financial statements for every listed issuer.
- More **rates and debt** data, historical and live.
- Efficient storage everywhere, plus systematic gap filling.

**Sourcing rule:** creative engineering is fine. That covers bulk files, official APIs, the public JSON endpoints a site's own pages use (called politely), XBRL/iXBRL parsing, and deriving a value from filings when no feed exists. Captchas, logins, paywalls, robots.txt disallow rules and IP blocks are never bypassed.

## W1. Earnings for every security (and the data-quality blockers)
**Upcoming dates**
- **US:** SEC 8-K Item 2.02 history for all filers, Nasdaq/NYSE public earnings-calendar JSON where permitted, Yahoo calendar in batches. All ~10k US listings.
- **Europe:** regulatory announcements (FCA NSM RNS "results" headlines, national OAMs, ESEF filing dates) plus the Yahoo calendar.
- **Japan:** TDnet / JPX schedule. **Hong Kong:** HKEX board-meeting notices (parsed).
- **Everywhere else:** the projected cadence, labelled as projected.

**Reactions and model expectations** for every issuer with prices and statements. Reactions need only prices, so they work for all ~24k securities.

**Remove the "Not covered" message:** widen `earnings_dates_fetch`, `earnings_events`, `earnings_reactions` and `earnings_expectations` to the `full` universe. Use incremental fetches under the per-job RAM budgets.

**Also, so the Saturday refresh can publish:**
- fix the data-quality failures:
  - 13F and short-interest freshness rules;
  - duplicate keys in news_stories and story_members;
  - the ratios point-in-time tolerance;
- fix the 7,656 duplicate primary keys in ml_labels.

## W2. Regulator-complete US / EU / UK issuers and financial statements
| Market | "Complete" means | Register / source of truth | Statements source |
|---|---|---|---|
| **US** | Every SEC registrant with listed equity: 10-K / 10-Q / 20-F / 40-F filers with a ticker | SEC `company_tickers_exchange.json` + EDGAR submissions | SEC **companyfacts bulk zip** (all CIKs, daily) + Financial Statement Data Sets (quarterly, back to 2009) |
| **EU** | Every issuer with equity admitted to an EU **regulated market** | **ESMA FIRDS** (all instruments, daily) joined to the GLEIF LEI register | **ESEF** annual reports from filings.xbrl.org + national storage mechanisms (OAMs); half-year reports from OAMs; national filings parsed where ESEF is missing |
| **UK** | Every issuer on the **FCA Official List** and AIM | FCA list + FIRDS (UK venues) + LSE public instrument lists | **Companies House free bulk accounts (iXBRL, daily/monthly zips, no key)** for UK-incorporated issuers + FCA NSM annual reports (ESEF-style iXBRL) for listed groups |

- **Coverage report** per market: issuers on the register, issuers in our universe, issuers with prices, with statements (latest annual, latest interim), and with sector. Every missing cell carries a reason.
- **Target:** at least 95% of regulated-market issuers with the latest annual statements, at least 85% with interims, and the remainder explained.

## W3. Rates and debt (historical + live)

**Sovereign curves (daily, full history where published)**
- US Treasury par yields and real yields (treasury.gov XML)
- Fed H.15
- ECB euro area yield curves (AAA and all issuers, spot/par/forward)
- Bundesbank Svensson parameters
- BoE nominal, real, inflation and OIS curves
- Japan MoF JGB rates
- Banque de France, Banca d'Italia and Banco de España benchmark yields
- **IGCP / Portugal** OT yields
- Swiss SNB, Canada BoC, Australia RBA
- 10-year yields for the rest of the world from OECD MEI and IMF

**Money markets:** SOFR, EFFR and OBFR (NY Fed API); €STR and EURIBOR (ECB, plus EMMI where public); SONIA (BoE); TONA (BoJ); policy rates (BIS).

**Credit**
- ICE BofA OAS by rating and sector (FRED, personal use)
- Moody's Aaa/Baa yields
- FINRA TRACE aggregate market stats
- **EU bond reference data** from FIRDS (every listed bond: issuer, coupon, maturity, currency) joined to issuers
- **US corporate bond reference data** from FINRA's public bond data pages, called politely

**Issuer debt, derived from filings**
- Debt by maturity bucket (XBRL/ESEF maturity tags)
- Interest expense, average cost of debt, interest coverage
- Net debt / EBITDA history
- Fixed vs floating where tagged
- Maturity walls per company and sector, and refinancing-risk metrics

**Sovereign debt**
- Debt / GDP and deficits: Eurostat, IMF WEO/GFS, OECD
- Issuance and auction calendars and results: TreasuryDirect API, national debt agencies
- TARGET2 balances: ECB

**Live**
- Delayed government bond yields (^TNX/^TYX/^FVX, Yahoo bund/gilt tickers where available)
- Official daily curves fetched right after publication through the startup-sync expected-time rules
- **UI:** Curve monitor (multi-country curves, spreads vs Bund, 2s10s / 5s30s, real vs nominal); Company 360 "Debt" panel (maturity wall, cost of debt, coverage); Credit page (OAS by rating and sector, issuance)

## W4. Storage and gap filling
- Each new dataset gets:
  - a contract;
  - a coverage.yaml entry with calendars, SLA, sources and fill strategy, so startup sync and the gap filler cover it automatically;
  - a field_availability row with reasons.
- **Storage:**
  - Parquet + ZSTD, dictionary-encoded strings, float32 where precision allows, decimals scaled to integers for rates (basis points × 100);
  - partitioned by region / year-month;
  - curves stored long, keyed by date, curve and tenor.
- History backfills run as resumable filler campaigns under the RAM budget.

## W2 implementation (2026-10-31, ingest-register)
**Registers (denominator), table `regulatory_register` (partition market), job `regulatory_register` (nightly root fetch).**
- US `sec_listed`: SEC `company_tickers_exchange.json` (Nasdaq / NYSE / Cboe / TXSE) x EDGAR full-index
  `master.gz` of the last 9 quarters (closed quarters cached as Parquet): a CIK is active when it filed a 10-K / 10-Q /
  20-F / 40-F (+/A) within 24 months. Kept as `excluded` with the reason: OTC-only, investment companies (N-CSR), new
  registrants without a periodic report yet, delinquent filers, and registrants whose only exchange lines are notes /
  preferred / trust units (Massive ticker types).
- EU `esma_rm`: ESMA FIRDS `FULINS_E` (weekly, ~12 MB zipped) parsed with a streaming lxml parser into
  `reference/regulatory/firds_esma_equity.parquet`; issuer = LEI of shares (CFI ES / EP / EC / EF / EL) admitted to an
  EEA regulated market at the issuer's request, not terminated. Regulated markets = ISO 10383 `RMKT` segments + the
  ESMA register (upreg MIR, which adds e.g. Zagreb). Depositary receipts and MTF / growth segments are out.
- UK `fca_official_list` / `lse_aim`: FCA FIRDS (data.fca.org.uk) with the same rule on UK RMs (XLON = Official List
  equity) and the AIM segment MIC AIMX. GLEIF gives names, legal country and the Companies House number.
- Raw FIRDS zips are deleted after parsing; the parsed file set is recorded in `data/state/regulatory_register.json`.

**Universe completeness.** CIK / LEI / ISIN matches through identifier_map. Missing EU / UK issuers become
`seeds/regulatory_register.csv` rows (ISIN -> OpenFIGI home / regulated-venue listing incl. preference shares,
investment trusts and the German regional exchanges -> verified Yahoo symbol; venues without a Yahoo market get
reference rows without a price symbol), loaded by `ref_universe` with the price-coverage seeds. `build_universe` adds
the primary security of every active register issuer to `full` (SPAC / fund SICs excluded; `BIG360_REGISTER_UNIVERSE=0`
switches the rule off). Found on the way: Massive leaves `primary_exchange` empty for the TXSE-listed stocks (Energy
Transfer, Dillard's, Texas Capital ...), so they had silently dropped out of the universe; the SEC exchange MIC is
now used.

**Statements.** `register_backfill` (process, 7.5 GB: `us_facts` companyfacts bulk zip for every register CIK in
chunks of 300, `us_fsds` FSDS for 20-F / 40-F filers, `normalize`) and `register_fetch` (process, 3 GB: `esef`
filings.xbrl.org index + every register-LEI filing not yet loaded, `fca_nsm`, `oam`, `ch` Companies House bulk
accounts, `yf` Yahoo fallback, `profile` Yahoo .info for sectors). Companies House: `ch_accounts` reads the Accounts
Data Product with HTTP Range requests (central directory + matched members only; a 3.4 GB monthly archive costs ~44 MB)
and parses iXBRL natively (`app.ingestion._ixbrl`); FRC `uk-core` concepts map through new `ifrs_uk` chains.
Coverage report: job `regulatory_coverage` -> `ops/regulatory_coverage`, docs/DATA_COMPLETENESS.md "Regulator
coverage", contracts/field_availability.yaml `regulator_coverage`; gap check `register_coverage` + filler campaigns
register_statements_us / _eu_uk / register_oam / register_yf_fallback / register_profiles.

**Storage.** FIRDS raw zips deleted after parsing (12 MB + 42 MB per week -> ~11 MB of Parquet); companyfacts zip
deleted once processed (sec_xbrl bulk + register_backfill); ESEF downloads no longer kept in the HTTP cache and the
landed compact copies are re-used - the processed cache (33.4 GB) was pruned to the 108 MB of landed copies;
Companies House: no zips stored at all, only the matched documents gzip-compressed.

**Results (2026-10-31 23:30, before -> after; register issuers US 5,496 / EU 3,295 / UK 1,437).** Universe US
99.3 -> 99.5 %, EU 36.6 -> 96.3 %, UK 25.0 -> 97.9 %. Latest annual US 48.2 -> 92.6 % (5,083 official), EU 35.8 ->
56.0 %, UK 23.7 -> 37.1 %. Interim US 45.3 -> 79.7 %, EU 15.5 -> 16.6 %, UK 2.1 -> 2.4 %. Prices EU / UK 37 / 25 %
until the nightly prices_global_yf fetches the ~3,000 new symbols' history; the Yahoo statements fallback
(register_yf_fallback, ~14 s / ticker) was started and continues as a filler campaign. Remaining reasons per cell:
docs/DATA_COMPLETENESS.md "Regulator coverage". Companies House bulk: 82 archives read, only 15 listed-issuer accounts
(listed PLCs file PDF accounts, not in the iXBRL product).

## Execution (agents)
- **A1 — W1** earnings for all, plus the DQ and ml_labels blockers.
- **A2 — W2** regulator-complete US/EU/UK issuers and statements.
- **A3 — W3** rates and debt.
- W4 rules apply to all three.
- Go-live is one app restart after each agent; backfills continue in the background.

## Progress
- [x] W1 Earnings for all + DQ / ml_labels blockers (2026-10-31). Sources used: Yahoo calendar (full universe,
  incremental due rules), SEC 8-K 2.02 / periodic filings, JPX schedule, TDnet tanshin (already ingested), HKEX
  board-meeting notifications, CNINFO report booking schedule (A-shares), FCA NSM results + notices of results,
  AFM / FSMA half-year reports; statutory-deadline projection for issuers without history. Rejected:
  api.nasdaq.com (robots `Disallow: /`), NYSE (no public calendar), new TDnet requests (www.release.tdnet.info
  robots `Disallow: /`; note: the existing tdnet_disclosures job fetches it - owner to review), CNINFO history
  (504s; Yahoo history used). DQ: 13F / FINRA calendar freshness, d-/p- story-file key guard, derived-period PIT
  guard + stale ratio-vintage purge, ml_labels dedupe + bounded memory; no threshold changed.
- [x] W2 Regulator-complete US / EU / UK + statements (2026-10-31; see "W2 implementation"; backfills continue as filler campaigns)
- [x] W3 Rates & debt (rates-w3 2026-10-31: app/rates, yield_curves 26 daily + 43 monthly curves (2.5M rows, ~8 MB ZSTD), auctions, FIRDS / OpenFIGI bonds, company_debt + walls, check published_dates, api 1.47.0, Curve monitor / Credit / Debt tab; see docs/it/DATA_LICENSING.md §2.8)
