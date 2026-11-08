# Smart money (roadmap v2 item #7): sources, formats, definitions

Who is buying, selling, shorting and crowding, per company and sector. Plan / tick boxes: `docs/ROADMAP_V2_PLAN.md`
(item 7 sub-steps), resumable state `data/state/roadmap_v2.json` key `item7`.

## 1. Sources (all official) and verified machine-readable formats (checked live 2026-10-19)

| Source | Table | URL / access | Format | History | PIT knowledge date |
|---|---|---|---|---|---|
| SEC Form 13F data sets | institutional_holdings | sec.gov "Form 13F Data Sets" window zips (TSV) | SUBMISSION / COVERPAGE / INFOTABLE | backfilled 2023Q1..2026Q2 (14 quarters) | filing date; analytics use the 45-day deadline cutoff |
| FINRA equity short interest | short_interest | cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv | pipe CSV | backfilled 2023-01..2026-09 | CDN Last-Modified (publication) |
| SEC fails-to-deliver | fails_to_deliver | sec.gov cnsfailsYYYYMM{a,b}.zip | pipe TXT | backfilled 2022-09.. | zip Last-Modified |
| SEC Schedule 13D / 13G | activist_filings | EDGAR full-index `master.gz` (quarterly) + daily-index `master.YYYYMMDD.idx`; filing header for ambiguous 13Ds | pipe index | 2022-01.. | Date Filed |
| UK FCA | eu_short_positions (fca) | `/publication/data/short-positions-daily-update.xlsx` (individual, frozen sheet "Historic Disclosures 10.07.2026"); `/publication/documents/aggregated-current-net-short-positions.csv`, `aggregated-historic-net-short-positions.csv` | xlsx / CSV (dd/mm/yyyy) | individual 2012-11..2026-07-10; aggregates since 2026-07 | position date + 2 business days (estimated) |
| France AMF | eu_short_positions (amf) | data.gouv.fr dataset `62738e33f1be79935d3e5553` (API resolves the daily CSV resource) | `;` CSV, UTF-8, holder LEI, start / end of publication | since 2012-11-01 | "Date de debut de publication" (observed) |
| Spain CNMV | eu_short_positions (cnmv) | `/DocPortal/Posiciones-Cortas/NetShortPositions.xls` | xls: sheets Fecha (publication date), Current, Series, Previous; issuer LEI | since 2012 | position date + 2 bd, capped by the file date |
| Netherlands AFM | eu_short_positions (afm) | `export.aspx?type=8a46a4ef-...&format=csv` (current), `type=3ca31b3d-...` (historic) | `;` CSV, cp1252 | since 2012 | position date + 2 bd |
| Ireland CBI | eu_short_positions (cbi) | `table-of-significant-net-short-positions-in-shares.xlsx` | xlsx sheets Current / Historical, values as fractions | since 2012 (last update 2026-06) | position date + 2 bd |
| Belgium FSMA | eu_short_positions (fsma) | `/en/de-shortselling?page&_format=csv` | CSV, decimal comma | current only (history accumulates nightly) | position date + 2 bd |
| Sweden FI | eu_short_positions (fi_se) | `/BlankningsRegister/GetAktuellFile`, `/GetHistFile` | ODS ("<0,5" = fell below 0.5%) | since 2012 | position date + 1 bd (published on receipt) |
| Norway Finanstilsynet | eu_short_positions (ftno) | `ssr.finanstilsynet.no/api/v2/instruments/export-json` (open API, OpenAPI at /api/v2/openapi.json) | JSON: instrument -> events (aggregate %, shares) -> active holder positions | last 2 years | position date + 1 bd |

All estimated knowledge dates are capped by the file publication date (CNMV) and our download date, and are never
before the position date. Robots / terms: every path above is allowed by the site's robots.txt (FCA disallows only
query strings on HTML pages; FSMA asks for a 30 s crawl delay - one request per run); one request per file per run.

### Skipped (with reason)
- **Italy CONSOB** - `PncPubbl.xlsx` (linked from consob.it/web/area-pubblica/pnc) answers with a Radware bot challenge
  (captcha) to automated clients: automated access refused.
- **Germany BaFin** - the net short positions are published only in the Bundesanzeiger; its robots.txt has
  `Disallow: /nlp` with the comment "NLP und Wartungsseite sollen nicht indexiert werden", i.e. the net short
  position pages. Not fetched; needs a user decision (a manual CSV export can be dropped into the raw folder later).
- **Denmark** - monthly xlsx snapshots of positions over 0.1% / 0.5% only, no current / historic register.
- **Finland** - HTML tables only.
- UK: since 10 Jul 2026 the FCA publishes **aggregated** net short positions per issuer only (no holder names). The
  frozen named-holder history stays searchable ("All notifications"); the current register shows FCA aggregates only
  (a regulator whose last named notification is > 30 days older than its aggregates is treated as aggregate-only).

## 2. Definitions (all MODEL-BASED items are labelled in the API and UI)

**13F point-in-time.** A quarter's snapshot uses only rows filed on or before the cutoff = filing deadline (period end
+ 45 days, next SEC business day; Presidents' Day aware) + 1 business day (EDGAR dates after-hours filings next day) or the run date while filings still arrive (`is_complete = false`); `knowledge_date =
cutoff`. The UI shows "positions as of quarter end Q, filed by D". Restatements filed after the deadline replace the
original rows in institutional_holdings and so drop out of the deadline snapshot (conservative).

**Splits**: 13F share counts are as reported; previous-quarter shares are re-expressed on the current share basis
(prices_daily cumulative split factors) before changes, flows and turnover are computed.

**Holder changes** (filers that filed both quarters): new, added (> +1%), trimmed (< -1%), unchanged, exited.
`net_flow_shares` = sum of share changes; `breadth` = (new + added - trimmed - exited) / positions compared.
`hhi` = sum of squared holder shares of the 13F total; `top10_share`.

**Hedge-fund-like filers** (transparent rule, model-based): 10 <= positions <= 1,000, AUM >= $50M, >= 60% of the
book in single stocks (mapped common / ADR / preferred / REIT units; excludes ETF-allocation wealth managers) and
(turnover >= 15% per quarter or option value share >= 5% or (top-10 weight >= 50% and turnover >= 5%)). Turnover = min(buys, sells) / mean AUM, buys / sells
= |share change| x price (so price moves are not turnover). Index-like books (> 1,000 positions) never qualify.

**Crowding** = number of hedge-fund-like filers holding the stock among their top-20 positions (`hf_top20_count`),
`crowding_pct` = its percentile within the quarter.

**Smart cohort** (model-based): each filer's reported quarter-end book is held for the next quarter (value weights,
adj_close; coverage >= 50% of AUM); excess = return minus the equal-weight mean of all filers. At quarter q,
`smart_score` = mean excess of books q-4..q-1 (>= 3) - all realised by period end q, before the cutoff. Cohort =
hedge-fund-like filers with smart_score > 0 in the top decile. Cohort flows = their share changes q-1 -> q.

**Short interest**: `si_pct_out` = short interest / shares OUTSTANDING (float is not published; documented proxy),
`days_to_cover` (FINRA), `si_chg_pct`, `si_z_1y` (vs previous 24 settlements). **Squeeze-risk flag** (model-based):
SI >= 15% of shares, days to cover >= 5, 20-day return >= +10%, shares <= 150M or market cap <= $2B. **FTD spike**
(model-based): largest daily fail >= 0.5% of shares outstanding (Reg SHO threshold level) or z >= 3 vs the previous 12
half-month files with >= 100k shares.

**Activism**: new 13D, 13D amendment, 13G -> 13D switch (filer's previous filing on the subject within 3 years was a
13G), new / amended 13G.

**EU / UK disclosed short %** per issuer at date D: the regulator aggregate when the regulator publishes aggregates
(FCA since 2026-07, Norway), else the sum of active holder positions (latest notification per holder >= 0.5%, within
2 years); max across regulators.

**Combined smart-money score** (model-based, `smart_money_signals`): robust z-scores within the date (winsorised 1/99%, (x - median) / 1.4826 MAD, clipped to ±4 since 2026-10-27) of
13F flow and breadth, smart-cohort flow, insider signal, minus short-interest level and change, minus EU short level and
30-day change; grouped (inst, smart, insider, short_interest, eu_short), score = mean of available groups,
`score_pct` 0..100. Forward-label-free. Optional input to the scoring Insider family behind
`contracts/scoring.yaml optional_inputs.smart_money.enabled` (default **false**; production scores unchanged).

## 3. Jobs
`eu_short_positions` (nightly), `activist_filings` (nightly; backfill `--since 2022-01-01`), `sec_13f` (weekly,
existing), `sec_13f_backfill` (manual, memory-bounded), `finra_short_interest` (weekly, existing; backfill
`--since 2022-12-31 --param ftd_n_latest=96`), `smart_money_13f` (weekly), `smart_money_signals` (nightly; history
`--param start=2023-06-30`, sanity report `--param ic_report=true`).

## 4. Sanity check of the combined score (2026-10-19, after the split adjustment; honest result)
`smart_money_signals --param start=2023-06-30 --param ic_report=true` -> `data/logs/reports/smart_money_ic_20261016.json`.
Spearman rank IC vs forward returns, point-in-time and purged: entry = first close after the signal date, exit h trading
days later, signal dates sampled at least h trading days apart (non-overlapping), 6,145 securities, 2023-06-30 ..
2026-10-16.

| column | IC 21d (t, n dates) | IC 63d (t, n dates) |
|---|---|---|
| score (composite) | -0.004 (-0.67, 33) | -0.005 (-0.56, 11) |
| g_inst (13F flow + breadth) | -0.015 (-1.30, 31) | -0.023 (-1.15, 10) |
| g_smart (smart-cohort flow) | -0.002 (-0.23, 27) | -0.019 (-1.16, 9) |
| g_insider | +0.014 (+0.79, 32) | +0.024 (+1.09, 11) |
| g_short_interest (low SI = bullish) | +0.016 (+1.39, 32) | +0.015 (+0.67, 10) |
| g_eu_short (low disclosed short = bullish) | +0.003 (+0.18, 31) | +0.011 (+0.42, 9) |

None is statistically significant; the composite has no predictive power in this short sample (the 13F history starts
2023Q1 and the smart cohort needs 4 quarters of track record). The short-interest and insider groups have the expected
sign; lagged 13F flows are slightly contrarian (consistent with crowding / the 45-day lag). Treat the score as a
descriptive screen, not an alpha signal; the backtesting lab can re-test it with longer history. The weights were NOT
tuned on this result (no data snooping).
