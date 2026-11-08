# Data completeness: summary (workstream C, docs/STARTUP_SYNC_PLAN.md section 3)

Audit 2026-10-27 to 2026-10-29. Read-only sweeps of the live API (http://127.0.0.1:8010): every BQL field of
`contracts/bql_fields.yaml` (816 fields, `POST /api/v1/bql`, compute-only, sequential) per universe x region, plus a
page-API sweep (18 Company 360 endpoints, 7 technicals endpoints and the conformal envelope for 20 representative
securities across 10 regions, and 22 page endpoints: markets, macro, cross-asset, earnings, smart money, insiders,
positioning; all 200). "Before" = snapshot `R20261026T063412JQJ9` (data 2026-10-23); "after" = snapshot
`R20261028T013840HPDK` (data 2026-10-27) with the fixes below. Some fixes reach the snapshot only at the next publish
(noted). The generated tables after the marker are re-created by `python -m app.qa.completeness --write`.

## Coverage before / after (universe full, % of securities with a value)

| Area | Fields | Median full % before | after | Mean full % before | after |
|---|---|---|---|---|---|
| Identity & classification | 21 | 96.1 | 96.1 | 68.3 | 69.4 |
| Prices & returns | 20 | 99.7 | 99.7 | 99.1 | 99.1 |
| Technicals | 211 | 97.3 | 97.3 | 86.6 | 86.5 |
| Valuation | 62 | 3.3 | 16.2 | 10.5 | 18.7 |
| Scores & factors | 72 | 3.2 | 3.2 | 3.5 | 3.9 |
| Insiders | 19 | 14.0 | 14.0 | 13.3 | 13.3 |
| News & sentiment | 22 | 18.2 | 19.2 | 13.1 | 12.9 |
| Smart money | 95 | 22.8 | 22.8 | 17.9 | 18.1 |
| Earnings | 65 | 10.5 | 10.5 | 25.9 | 25.9 |
| Fundamentals | 165 | 4.3 | 14.9 | 8.7 | 12.8 |
| Sector KPIs | 54 | 0.2 | 0.6 | 0.2 | 0.8 |
| Macro exposures | 6 | 84.3 | 84.3 | 84.3 | 84.3 |
| Other | 4 | 100.0 | 100.0 | 100.0 | 100.0 |

| Field | Full % before → after | US | EU | UK | JP | KR | CN | HK | APAC | LATAM |
|---|---|---|---|---|---|---|---|---|---|---|
| `roic` | 3.5 → 15.9 | 8→37 | 8→80 | 12→77 | 1→4 | 1 | 0 | 1 | 1 | 17 |
| `croic` | 3.3 → 14.7 | 8→35 | 8→72 | 12→73 | 1→4 | 1 | 0 | 1 | 1 | 15 |
| `pe_ttm` | 3.0 → 12.2 | 8→34 | 6→56 | 7→23 | 1 | 0 | 0 | 0 | 0 | 10 |
| `ev_ebit` | 3.0 → 12.5 | 8→35 | 6→59 | 8→24 | 1 | 0 | 0 | 0 | 0 | 10 |
| `fcf_yield` | 2.7 → 13.1 | 8→38 | 5→56 | 6→27 | 1 | 1 | 0 | 1 | 0 | 9 |
| `gross_margin` | 2.9 → 13.5 | 7→31 | 6→65 | 9→66 | 1→5 | 1 | 0 | 1 | 0 | 16 |
| `mkt_cap_usd` | 16.8 → 16.8 | 48 | 69 | 37 | 1 | 1 | 0 | 1 | 1 | 17 |
| `si.pct_float` | 0.0 → 11.0 | 0→47 | 0→0 | 0 | 0 | 0 | 0 | 0 | 0 | 0→1 |
| `si.days` | 24.0 → 23.8 | 99 | 2→2 | 9→5 | 0 | 0 | 0 | 0 | 0 | 11→7 |
| `sm.z_smart_flow` | 9.8 → 9.8 | 41 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| `f.value.chg_1m` | 0.0 → 3.2 | 0→9 | 0→6 | 0→8 | 0→1 | 0→1 | 0 | 0→1 | 0 | 0→10 |
| `sector` | 70.3 → 81.9 | 27→77 | 100 | 99 | 100 | 83 | 100 | 1 | 100 | 78 |
| `kpi.revenue_per_employee` | 0.0 → 0.8 | 0→1 | 0→8 | 0→9 | 0 | 0 | 0 | 0 | 0 | 0→0 |
| `eps_surprise` | 8.7 → 8.7 | 34 | 2 | 4 | 0 | 0 | 0 | 0 | 0 | 13 |
| `cf.touch_20d` | 84.3 → 84.3 | 45 | 97 | 94 | 98 | 96 | 98 | 92 | 99 | 68 |
| `tp.vol_regime` | 83.4 → 83.4 | 98 | 4 | 4 | 99 | 99 | 99 | 98 | 1 | 80 |
| `tech.hi52_pct` | 73.7 → 73.3 | 84→83 | 95 | 99 | 61→60 | 54 | 99 | 35 | 60 | 52 |
| `px_last` | 99.9 → 99.9 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 |

Fundamental areas are low in universe full by design of the data: 23.8k securities, of which ~4.8k issuers have
loaded statements; CN / KR / HK / APAC have prices only (reasons in the tables below: `needs_key` OPENDART for Korea,
`no_source` HKEX PDF-only / APAC, `source_unavailable` BaoStock). Within mvp_us / mvp_global the fundamentals cover
85-100 %.

## Bugs and mapping gaps fixed

| # | Issue | Root cause | Fix | Result |
|---|---|---|---|---|
| 1 | `si.pct_float` empty for every security (BQL, screener) | FINRA publishes no float; `short_interest.short_pct_float` was never computed | serving `short_interest_latest.short_pct_float` = short interest / shares outstanding (si_signals float proxy); BQL `si.pct_float` -> `sm_si_latest.si_pct_out` (+ history) | US 0 % -> 47 % (= every US listing with shares outstanding); BQL change live after the API restart |
| 2 | FINRA symbol resolution "17 %" | the 17 % is of all FINRA rows (9.5k OTC + ~4k funds not in the universe). Real gaps: class shares published compact (BFB, HEIA), ETFs (no ticker_mic), market class SC (Nasdaq Capital Market, 1,666 symbols) mapped to XNYS, ADR rows stored on the HOME listing (primary_security_id: ASML, BABA, ABEV short interest landed on ASML.AS ...), ticker reuse | `app.normalize.us_symbols` (compact class-share, ETF, OpenFIGI-cache and dated FTD-history maps); finra_si keeps the listing security + SC -> XNAS; identity backfill re-points same-issuer rows to the US line and returns rows outside a security's trading life to the provisional key | active US listings + ETFs with SI at the latest settlement 93.4 % -> 96.8 %; 39,488 SI rows re-resolved, 2,371 ticker-reuse rows (85 symbols) un-resolved, 109,072 FTD rows resolved |
| 3 | `sm.z_smart_flow` ~constant (-0.14) | mean / std z of extremely heavy-tailed flows (max 1,047 vs median 0; mixed units) | robust z: (x - median) / 1.4826 MAD, clipped +-4, for every smart-money component | IQR -0.15..-0.11 -> -0.43..0.98, 242 -> 474 distinct values (z_inst_flow likewise); in the next snapshot |
| 4 | ROIC outliers (EXPE 1,498 %, NTAP 357 %, RR.L 196 %, ASML 606 %) and JP ROIC ~0 | (a) USD NOPAT divided by local-currency invested capital (JP 1/150, EUR / GBP x FX); (b) Yahoo "Invested Capital" (= total debt on quarterly rows) overrode the derived definition for 1,001 issuers; (c) net-of-cash invested capital near zero for cash-rich / customer-float businesses | local-basis twin of USD metrics; Yahoo chain removed + 1,001 issuers re-normalised; bounds: roic / croic nulled when abs > 200 % or avg IC < 5 % of assets, with `roic_flag` / `roic_raw` metric rows and a `suppressed` reason; serving does not fall back to an older ROIC when the latest is withheld | Toyota 0.04 % -> 7.2 %, Sony 13.9 %, ASML 606 % -> 63.6 %, RR.L 196 % -> withheld, EXPE / NTAP latest TTM withheld (raw 1,498 % / 357 %; the current snapshot still shows their last unflagged period, the next publish shows the withheld reason) |
| 5 | Ratios / valuation computed for ~800 issuers although statements are loaded for ~4.8k | jobs ran with the mvp universe default | jobs.yaml v40: ratios_kpis + valuation_daily universe=full (rebuild run done) | roic US 8 -> 37 %, EU 8 -> 80 %, UK 12 -> 77 %; pe_ttm US 8 -> 34 %, EU 6 -> 56 % (valuation history for the newly covered names starts 2026-10-12; mode=history for a backfill) |
| 6 | factor `delta_1m` (company factors, `f.*.chg_1m`) null for every security | reference row looked up 28 days back, backward only, max 50 days: misses between month-ends | nearest row 20..50 days back on either side | 0 -> 7,640 of 7,810 scored rows |
| 7 | US sector / subsector 27 % | SEC SIC codes mapped only for insider-active issuers | SIC backfill for every CIK issuer (3,000 per night) | US 27 % -> 77 %; KR (452) and LATAM (77) native names mapped by new KSIC / CVM rules (next ref_universe run) |
| 8 | `revenue_per_employee` empty everywhere | employees rarely tagged in statements | issuer-profile employees for the latest ~13 months | 0 -> 0.8 % of universe (all IT-services KPI-set names with a count) |
| 9 | Smart-money overview rows carry only their own dataset | each list joined one table | lists join 13F, short interest and score of every row | accumulation / distribution lists: SI and score fields were 100 % null; filled after the API restart (fixture-tested) |
| 10 | BQL: duplicate `name` column; `group().agg()` could not `sort(n)` | key columns repeated as values; n not an output reference | key-duplicate fields dropped; `sort(n)` on the group size | live after the API restart |

The SEC acceptance-time item of section 3 was fixed by workstream B. JP prices "stuck at 2026-10-13": no longer true
(3,696 of 3,709 JP names at the 2026-10-27 session).

## Remaining gaps (reason and plan)

| Gap | Reason | Plan / owner |
|---|---|---|
| Fundamentals, valuation, scores for KR (2,655 names) | `needs_key` OPENDART_APIKEY | user adds the key; DART adapter exists |
| CN statements (5,224 names) | `source_unavailable` BaoStock IP ban; AKShare covers the curated names | ingest-global: resume BaoStock with one worker |
| HK statements and sectors (2,461) | `no_source` HKEX PDF only; HSICS not a free feed | Yahoo profile sector for HK (optional, unofficial) |
| APAC statements (IN / AU / SG / TH / ID) | `no_source` | - |
| US small caps without statements (~3k) and shares outstanding for prices-only names | coverage v2 W2 (2026-10-31): statements for every SEC listed periodic filer (register_backfill us_facts: companyfacts bulk zip for all register CIKs); JP / KR / CN / HK shares outstanding still `no_source` | see "Regulator coverage" below; JP / KR / CN / HK shares feed open |
| Scores / ML outside mvp_us / mvp_global | done 2026-10-30 (scores); ML withheld (`suppressed`) | see "Full-universe scores & ML" |
| JP earnings surprises (0.1 %) | done 2026-10-31 (COVERAGE_V2 W1): TDnet tanshin releases are earnings_events; surprises from the seasonal random walk (the tanshin's prior-year comparative) until 10 quarters accrue; now `insufficient_data` per company | earnings (W1) |
| EU / UK quarterly EPS surprises | `no_source` (semi-annual reporting; ESEF covers annual reports) | - |
| Earnings dates / reactions outside the earnings universe | done 2026-10-31 (COVERAGE_V2 W1): universe `full` (Yahoo + HKEX BMN + CNINFO + FCA NSM + statutory-deadline projection); each remaining gap carries the company's own reason (no release recorded / no statements / not announced) | earnings (W1) |
| Conformal / macro betas US 45 %, LATAM 68 % | `failed`: conformal_envelopes timed out on 10-24/25 (before the 10-26 fix) | next sync / nightly run |
| Vol regime EU / UK / APAC, tech factors stale regions | `stale`: missing latest session bars at build time | startup sync (agent A) |
| ~760 delisted / acquired US names in universe full | `stale` per entity ("No trades since ...") | ref-identity: retire them |
| ISIN JP / CN, LEI outside EU / UK, FIGI outside US | `no_source` / `out_of_scope` | JPX / CSDC lists, GLEIF, OpenFIGI refresh |
| DE / IT short positions | `no_source` (robots / captcha; user decision) | - |
| FINRA settlement 2026-10-15 | `not_yet_published` / `due` at audit time (watermark 09-30) | startup sync |

## Full-universe scores & ML (2026-10-30)

Request: factor scores, composites, grades, ranks and ML forecasts for every security, not only mvp_us / mvp_global,
with the MVP results unchanged. State: data/state/full_scope.json; scratch / evidence: scratchpad/full_scope/.

**Scores (live).** New job `scoring_full` (app.analytics.scoring_full, jobs.yaml v41, nightly after `scoring`, sync
pass 2). Every equity outside the MVP universe (the serving `u_full` set) is scored against the MVP reference
cross-section of the same as_of date (the MVP pipeline is re-run in memory, identical to `scoring`): each metric
percentile is the position the security would take if it were added alone to the MVP peer group (leave-one-in, same
level fallbacks and min 8 peers), the valuation regression of its peer group is fitted on the MVP peers, factor
re-percentiles likewise, grade = the MVP quintile its composite would take; self-history scores are per security.
Ranks of extended rows (rank_global, rank_sector, factor rank_in_sector) are ranks among all scored securities of the
date within region (peer region) x sector, one row per issuer; the serving layer adds `rank_*_full` for every scored
security (MVP rows keep their MVP ranks). Non-MVP listings of MVP issuers copy the issuer's MVP rows. Tables
factor_scores_ext / composite_scores_ext / peer_scores_ext (MVP tables untouched). A null composite has reason
`insufficient_data` (new code; sync code no_source) with the missing inputs per factor (no statements, no sector, no
market cap, stale prices, short history, ...); issuers without an industry classification get their market factors
(momentum, insider, sentiment, macro) through a pseudo kpi set. Incremental: per-security input fingerprints (prices,
valuation, ratios, KPIs, insider, sentiment, moat, forecast, betas, classification) and a digest of the MVP
reference; same as_of and same reference -> only changed securities are re-scored (live: 0 of 23,828 in 17 s), else
all (82 s). Chunked by peer region (3,000 issuers), peak 2.2-3.5 GB.

MVP regression: the original scoring.py and the new code on the same lake state give identical peer / factor /
composite rows (2026-10-29 and 2026-10-30; float tolerance 1e-9, ties). One deliberate change: the scoring security
of an issuer with two equally-primary listings was random per run (unique() without order); it is now deterministic.

Coverage on the live API (BQL, universe full, 23,828 securities), snapshot R20261030T144122JG9D (data 2026-10-30, before) -> SI20261031T0619213D3X (data 2026-10-30) after; % of securities with a value:

| Group | n | composite | grade | rank_global | f.quality | f.value | f.momentum | ml.rank_pct_3m |
|---|---|---|---|---|---|---|---|---|
| all | 23,828 | 3.2 -> 18.3 | 3.2 -> 18.3 | 3.2 -> 18.3 | 3.2 -> 18.1 | 3.2 -> 15.2 | 3.2 -> 97.8 | 3.3 -> 3.3 |
| scope: mvp | 771 | 99.0 -> 98.8 | 99.0 -> 98.8 | 99.0 -> 98.8 | 98.7 -> 98.6 | 97.9 -> 97.8 | 99.2 -> 99.1 | 100.0 -> 100.0 |
| scope: non_mvp | 23,057 | 0.0 -> 15.6 | 0.0 -> 15.6 | 0.0 -> 15.6 | 0.0 -> 15.4 | 0.0 -> 12.5 | 0.0 -> 97.7 | 0.0 -> 0.0 |
| region: APAC | 1,994 | 0.5 -> 0.7 | 0.5 -> 0.7 | 0.5 -> 0.7 | 0.5 -> 0.7 | 0.5 -> 0.7 | 0.5 -> 99.5 | 0.5 -> 0.5 |
| region: CN | 5,224 | 0.4 -> 0.4 | 0.4 -> 0.4 | 0.4 -> 0.4 | 0.4 -> 0.4 | 0.4 -> 0.4 | 0.4 -> 99.1 | 0.4 -> 0.4 |
| region: EU | 1,385 | 6.5 -> 93.2 | 6.5 -> 93.2 | 6.5 -> 93.2 | 6.5 -> 90.5 | 6.3 -> 66.3 | 6.4 -> 98.7 | 6.5 -> 6.5 |
| region: HK | 2,461 | 0.7 -> 1.1 | 0.7 -> 1.1 | 0.7 -> 1.1 | 0.7 -> 1.1 | 0.6 -> 1.0 | 0.7 -> 96.2 | 0.7 -> 0.7 |
| region: JP | 3,709 | 1.1 -> 5.6 | 1.1 -> 5.6 | 1.1 -> 5.6 | 1.1 -> 5.6 | 1.0 -> 1.2 | 1.1 -> 99.1 | 1.1 -> 1.1 |
| region: KR | 2,655 | 0.6 -> 0.7 | 0.6 -> 0.7 | 0.6 -> 0.7 | 0.6 -> 0.7 | 0.6 -> 0.7 | 0.6 -> 98.1 | 0.6 -> 0.6 |
| region: LATAM | 438 | 10.3 -> 16.7 | 10.3 -> 16.7 | 10.3 -> 16.7 | 9.8 -> 15.5 | 10.3 -> 16.7 | 10.5 -> 86.8 | 10.5 -> 10.5 |
| region: UK | 354 | 7.6 -> 87.3 | 7.6 -> 87.3 | 7.6 -> 87.3 | 7.6 -> 90.1 | 7.6 -> 32.8 | 7.6 -> 99.4 | 7.6 -> 7.6 |
| region: US | 5,604 | 9.0 -> 42.8 | 9.0 -> 42.8 | 9.0 -> 42.8 | 9.0 -> 42.6 | 8.9 -> 42.8 | 9.0 -> 96.0 | 9.1 -> 9.1 |
| size: adv1-10m | 5,124 | 0.0 -> 6.0 | 0.0 -> 6.0 | 0.0 -> 6.0 | 0.0 -> 6.2 | 0.0 -> 0.0 | 0.0 -> 97.9 | 0.0 -> 0.0 |
| size: adv<1m | 9,126 | 0.0 -> 2.7 | 0.0 -> 2.7 | 0.0 -> 2.7 | 0.0 -> 2.7 | 0.0 -> 0.0 | 0.0 -> 97.1 | 0.0 -> 0.0 |
| size: adv>=10m | 5,556 | 0.0 -> 3.2 | 0.0 -> 3.2 | 0.0 -> 3.2 | 0.0 -> 3.3 | 0.0 -> 0.0 | 0.0 -> 98.5 | 0.0 -> 0.0 |
| size: mcap_large | 1,245 | 60.6 -> 98.3 | 60.6 -> 98.3 | 60.6 -> 98.3 | 60.4 -> 97.5 | 59.9 -> 97.4 | 60.7 -> 99.3 | 61.1 -> 61.4 |
| size: mcap_mid | 1,245 | 1.0 -> 88.9 | 1.0 -> 88.9 | 1.0 -> 88.9 | 1.0 -> 87.5 | 1.0 -> 89.1 | 1.0 -> 99.4 | 1.0 -> 1.0 |
| size: mcap_small | 1,520 | 0.0 -> 85.6 | 0.0 -> 85.6 | 0.0 -> 85.6 | 0.0 -> 83.9 | 0.0 -> 86.2 | 0.0 -> 97.3 | 0.0 -> 0.0 |

Size: mcap_* = market-cap bucket where shares outstanding are known, else the 20-day ADV bucket (no market cap). Region shares stay low where no statements are loaded (CN, KR, HK, APAC; KR needs OPENDART_APIKEY): those names get momentum / insider / sentiment / macro factor scores and an insufficient_data composite. MVP 99.0 -> 98.8 %: 2 MVP names lost their composite on the 2026-10-30 data (nightly inputs; the MVP code path is unchanged, regression identical).

**ML (decision: keep training on the MVP universe; no out-of-universe predictions).** Purged walk-forward (production
folds: 5-year initial window, 24-month test folds, purge = horizon + 10 d) on the production feature registry, 2005 to
2026-10, 24,587 priced securities. A = production recipe (MVP training, OOS on MVP names = the production numbers;
the same fold models extrapolated to every other security with each feature rank-gauss-transformed against the MVP
cross-section of the date). B = trained on every security with ADV20 >= $1m at the date (PIT; PIT market caps exist
for MVP names only, so the suggested mcap > $300m filter could not be applied historically), 10.9 M liquid rows, at
most 3 M training rows per fold (all MVP rows + a seeded sample, 6 GB cap), xgboost n_jobs 8. Labels for B / extended
names: fwd_excess_sector against the liquid universe's sector x region mean. Size buckets = PIT 20-day ADV (large
>= $50m, mid $10-50m, small $1-10m, micro < $1m).

| Subset (OOS rank IC, Newey-West t) | 1M | 3M | 6M |
|---|---|---|---|
| MVP names, A = production recipe (MVP label) | +0.035 (+7.2) | +0.034 (+4.0) | +0.022 (+1.5) |
| MVP names, B = broad liquid (MVP label) | -0.012 (-1.8) | -0.011 (-1.0) | -0.013 (-0.8) |
| MVP names, B - A paired | -0.047 (-6.9) | -0.044 (-5.1) | -0.034 (-3.8) |
| Extended, A extrapolated (all priced names) | -0.005 (-1.5) | -0.008 (-1.5) | -0.007 (-0.6) |
| Extended liquid (ADV >= $1m), A extrapolated | -0.001 (-0.2) | -0.001 (-0.2) | -0.002 (-0.1) |
| Extended liquid, B | +0.101 (+21.3) | +0.128 (+18.0) | +0.146 (+16.0) |

| region | A MVP 1M | A ext 1M | B ext 1M | A MVP 3M | A ext 3M | B ext 3M | A MVP 6M | A ext 6M | B ext 6M |
|---|---|---|---|---|---|---|---|---|---|
| US | +0.025 (+4.2) | -0.024 (-5.7) | +0.072 (+9.3) | +0.034 (+3.8) | -0.044 (-5.7) | +0.107 (+8.3) | +0.029 (+2.0) | -0.033 (-1.8) | +0.139 (+8.1) |
| EU | +0.012 (+1.5) | -0.001 (-0.2) | +0.028 (+3.6) | +0.042 (+3.6) | -0.006 (-0.8) | +0.038 (+3.0) | +0.057 (+2.6) | -0.015 (-1.8) | +0.059 (+3.5) |
| UK | +0.024 (+1.9) | +0.016 (+3.0) | +0.019 (+2.8) | +0.047 (+2.2) | +0.015 (+1.5) | +0.016 (+1.3) | +0.054 (+1.6) | +0.008 (+0.6) | +0.037 (+2.3) |
| JP | +0.024 (+2.5) | -0.009 (-1.6) | +0.080 (+10.7) | +0.051 (+3.2) | -0.008 (-1.1) | +0.103 (+9.2) | +0.033 (+1.4) | -0.012 (-1.0) | +0.128 (+8.2) |
| KR | - | +0.022 (+4.3) | +0.127 (+21.0) | - | +0.033 (+4.4) | +0.152 (+14.8) | - | +0.032 (+2.5) | +0.172 (+12.2) |
| CN | - | +0.029 (+5.1) | +0.090 (+11.0) | - | +0.027 (+3.3) | +0.108 (+10.0) | - | +0.027 (+2.9) | +0.114 (+8.3) |
| HK | +0.006 (+0.3) | -0.008 (-1.6) | +0.086 (+8.6) | +0.007 (+0.2) | -0.024 (-2.8) | +0.128 (+7.7) | +0.080 (+1.9) | -0.029 (-1.7) | +0.168 (+7.9) |
| APAC | - | -0.033 (-5.7) | +0.093 (+12.8) | - | -0.043 (-5.7) | +0.116 (+11.3) | - | -0.040 (-3.8) | +0.141 (+11.7) |
| LATAM | +0.034 (+2.9) | -0.014 (-3.1) | +0.057 (+6.0) | +0.008 (+0.4) | -0.016 (-1.7) | +0.085 (+5.2) | +0.000 (+0.0) | +0.006 (+0.4) | +0.103 (+3.8) |

| bucket | A MVP 1M | A ext 1M | B ext 1M | A MVP 3M | A ext 3M | B ext 3M | A MVP 6M | A ext 6M | B ext 6M |
|---|---|---|---|---|---|---|---|---|---|
| large | +0.019 (+3.1) | -0.007 (-1.2) | +0.121 (+16.2) | +0.021 (+2.7) | -0.021 (-2.8) | +0.154 (+13.3) | +0.018 (+1.5) | +0.004 (+0.3) | +0.172 (+12.3) |
| mid | +0.018 (+2.2) | -0.009 (-2.3) | +0.110 (+21.5) | +0.012 (+1.0) | -0.014 (-2.2) | +0.135 (+17.6) | -0.016 (-0.8) | +0.001 (+0.1) | +0.147 (+14.9) |
| small | +0.057 (+4.0) | -0.004 (-1.0) | +0.084 (+18.6) | +0.029 (+1.5) | -0.001 (-0.1) | +0.109 (+16.3) | -0.036 (-1.7) | -0.005 (-0.5) | +0.127 (+14.4) |
| micro | - | -0.018 (-4.5) | - | - | -0.035 (-5.6) | - | - | -0.037 (-2.9) | - |

| Leakage check (train on labels shuffled within date, 36 m before a purge gap, test last 6 m) | 1M | 3M | 6M |
|---|---|---|---|
| B broad liquid: shuffled-label IC seed0 / seed1 -> pass? | -0.006 / -0.003 -> pass | -0.037 / -0.029 -> FAIL | +0.124 / -0.056 -> FAIL |
| A extrapolated: shuffled-label IC seed0 / seed1 -> pass? | -0.069 / +0.009 -> FAIL | -0.116 / -0.067 -> FAIL | -0.027 / -0.183 -> FAIL |


Gate: B degrades MVP performance at every horizon (paired B - A t -3.8 to -6.9) and fails the label-permutation
leakage check at 3M and 6M; its 0.10-0.15 IC on extended names with 90-97 % hit rates is implausible and treated as
leakage / an untradeable microstructure artefact until proven otherwise -> not production. A has no skill outside the
MVP universe overall (-0.005 to -0.008) and the permutation check of the extrapolation path fails at every horizon
(shuffled-label |IC| up to 0.18), so none of its segments is eligible even where the segment's own IC passed t >= 2
(e.g. KR, CN 1M) -> `ml_predict_full` serves NO extrapolated predictions: every non-MVP security has an
ml_scope_status row (`suppressed` with the evidence, `insufficient_data` for < 252 sessions, `stale` without a recent
bar), shown as the tooltip of empty ml.* fields and in the Model tab. The production gate itself had a bug (its own
rejection note quoted the pass mark, so the demoted FT-Transformer was production for 1M / 3M since 2026-10-25): fixed,
flags repaired; production is xgboost_rank for 1M / 3M / 6M; the MVP ml_blend composite now uses the 3M xgboost model.

**Serving / API / UI (api 1.44.0, additive).** Screener (universe full: rank among all scored securities, `ext`
marker, insufficient-data tooltip), Company 360 (header "Extended scope" chip, composite reason, Model tab
"Extrapolated" banner / reason), BQL `composite`, `grade`, `rank_*`, `f.*` for every scored security plus new
`rank_full`, `rank_sector_full`, `n_sector_full`, `score_scope`, `score_missing`, `f.<factor>.rank_sector_full`,
`ml.extrapolated_<h>`, `ml.status_<h>`. The factor-weighted composite fallback no longer fills an explained null.

**Run times / memory (live, 2026-10-31).** scoring_full 82-92 s full, 17-22 s incremental, peak 2.2-3.5 GB
(cap 4 GB); ml_predict_full 8-23 s (status only); composite_ml_blend 4-6 s; section publish 19 min (all sections dirty
after the code change; a 7 GB DuckDB budget, as the full build). ML experiment: features 24,587 securities x 1,276
dates (19.7 M rows, 6 min), labels 2 min, walk-forward 3 horizons x 9 folds x (A + B) 28 min peak 6.0 GB, leakage
checks 12 min peak 6.1 GB (xgboost n_jobs 8).

**Go-live.** Restart the worker (jobs.yaml v41: scoring_full / ml_predict_full in the nightly DAG; it also still holds
the pre-fix serving module, so its scheduled builds fail until restarted) and the API (api 1.44 fields, BQL fields,
availability reasons), `cd frontend && npm run build:app`, then `python -m app.qa.completeness --write` once more so
the 21 new BQL fields get availability rows. The startup sync already runs the new jobs (fresh process per sync).

<!-- regulator-coverage:start -->
## Regulator coverage

Coverage v2 W2 (docs/COVERAGE_V2_PLAN.md). Generated by job `regulatory_coverage` on 2026-11-08. Denominator: the securities regulators' registers (`regulatory_register`): SEC exchange-listed 10-K / 10-Q / 20-F / 40-F filers (company_tickers_exchange + EDGAR full-index), issuers of shares admitted to an EEA regulated market at their request (ESMA FIRDS + ISO 10383 / ESMA regulated-market list), UK regulated market = FCA Official List equity (FCA FIRDS, venue XLON) and AIM (FCA FIRDS, AIMX). Per issuer row and reason: `ops/regulatory_coverage`; machine-readable summary: `contracts/field_availability.yaml` `regulator_coverage`. Annual = latest FY within 12 months + the register's filing deadline (+0.5 month); interim = latest Q1-Q3 / H1 within 5 months (US) / 10.5 months (EU / UK). Targets: annual >= 95 %, interim >= 85 %.

| Market | Register issuers | In universe | Prices | Latest annual (official) | Latest interim | Sector |
|---|---|---|---|---|---|---|
| US (SEC) | 5,491 | 5,459 = 99.4 % (before 99.3) | 5,459 = 99.4 % (before 99.3) | 5,090 = 92.7 % (before 48.2) (5,084 official) | 4,492 = 81.8 % (before 45.3) | 5,150 = 93.8 % (before 93.7) |
| EU / EEA (ESMA FIRDS) | 3,293 | 3,230 = 98.1 % (before 36.6) | 1,206 = 36.6 % (before 36.6) | 2,087 = 63.4 % (before 35.8) (1,172 official) | 924 = 28.1 % (before 15.5) | 2,668 = 81.0 % (before 36.6) |
| UK (FCA FIRDS) | 1,438 | 1,425 = 99.1 % (before 25.0) | 359 = 25.0 % (before 25.0) | 779 = 54.2 % (before 23.7) (424 official) | 46 = 3.2 % (before 2.1) | 1,162 = 80.8 % (before 24.4) |

| Register | Issuers | Universe % | Prices % | Annual % | of which official % | Interim % | Sector % |
|---|---|---|---|---|---|---|---|
| SEC listed periodic filers | 5,491 | 99.4 | 99.4 | 92.7 | 92.6 | 81.8 | 93.8 |
| EEA regulated markets | 3,293 | 98.1 | 36.6 | 63.4 | 35.6 | 28.1 | 81.0 |
| FCA Official List (LSE Main Market) | 845 | 98.6 | 37.2 | 68.6 | 49.5 | 4.7 | 69.1 |
| AIM | 593 | 99.8 | 7.6 | 33.6 | 1.0 | 1.0 | 97.5 |

Every missing cell has a reason (code: note). Most frequent reasons per market:

| Market | Cell | Missing | Reasons (count) |
|---|---|---|---|
| US | universe | 32 | mapping_gap: CIK not in the reference universe (no exchange listing record in the Massive / SEC lists) (32) |
| US | prices | 32 | out_of_scope: issuer not in the universe yet (see universe reason) (32) |
| US | annual | 401 | not_yet_published: no 10-K / 20-F / 40-F filed yet (only 10-Q since listing) (185); pipeline_bug: companyfacts loaded but no core line item normalised (custom tags / unit filter) (120); stale: latest annual FY <date> ; the newer one is not loaded yet (53); out_of_scope: issuer not in the universe yet (statements are loaded but cannot be normalised) (21) |
| US | interim | 999 | not_applicable: foreign private issuer (20-F / 40-F): no 10-Q duty; 6-K interims are rarely XBRL-tagged (752); stale: latest quarter <date> (186); out_of_scope: issuer not in the universe yet (24); pipeline_bug: 10-Q filed but no quarterly core item normalised (21) |
| US | sector | 341 | not_applicable: blank-check company (SIC 6770, SPAC) has no operating sector (268); mapping_gap: no SIC / Yahoo industry mapped to the sector taxonomy (51); out_of_scope: issuer not in the universe yet (22) |
| EU | universe | 63 | unmapped: OpenFIGI has no equity listing on the issuer's regulated / home venue (61); no_source: Yahoo has no bars for WISA.HE, WIS-A.HE (suspended / delisted / unlisted share line) (1); no_source: Yahoo has no bars for CDLR.OL, CDL-R.OL (suspended / delisted / unlisted share line) (1) |
| EU | prices | 2,087 | due: new listing, first price history fetch pending (prices_global_yf / gap filler) (1,566); no_source: no Yahoo / Polygon symbol for the primary listing (458); out_of_scope: issuer not in the universe yet (see universe reason) (63) |
| EU | annual | 1,206 | no_source: no ESEF / national OAM filing found for this LEI and no Companies House iXBRL accounts; Yahoo statements fallback pending (regist (682); stale: latest annual FY <date> ; the newer one is not loaded yet (424); out_of_scope: issuer not in the universe yet (statements are loaded but cannot be normalised) (63); pipeline_bug: official facts loaded but no core line item normalised (taxonomy / unit mapping) (37) |
| EU | interim | 2,369 | no_source: half-year reports are not ESEF-tagged (Transparency Directive: annual only) and the OAMs publish them as PDF; Yahoo quarterly fal (2,273); out_of_scope: issuer not in the universe yet (63); stale: latest interim <date> ; the next half-year is not loaded yet (33) |
| EU | sector | 625 | mapping_gap: no SIC / Yahoo industry mapped to the sector taxonomy (562); out_of_scope: issuer not in the universe yet (63) |
| UK | universe | 13 | unmapped: OpenFIGI has no equity listing on the issuer's regulated / home venue (12); no_source: Yahoo has no bars for AXV.L (suspended / delisted / unlisted share line) (1) |
| UK | prices | 1,079 | due: new listing, first price history fetch pending (prices_global_yf / gap filler) (1,066); out_of_scope: issuer not in the universe yet (see universe reason) (13) |
| UK | annual | 659 | no_source: AIM companies are not on the FCA NSM / ESEF; Companies House holds PDF accounts only (not in the iXBRL bulk product); Yahoo fallb (367); no_source: no ESEF / national OAM filing found for this LEI and no Companies House iXBRL accounts; Yahoo statements fallback pending (regist (201); stale: latest annual FY <date> ; the newer one is not loaded yet (67); out_of_scope: issuer not in the universe yet (statements are loaded but cannot be normalised) (13) |
| UK | interim | 1,392 | no_source: half-year reports are not ESEF-tagged (Transparency Directive: annual only) and the OAMs publish them as PDF; Yahoo quarterly fal (1,375); out_of_scope: issuer not in the universe yet (13); stale: latest interim <date> ; the next half-year is not loaded yet (4) |
| UK | sector | 276 | mapping_gap: no SIC / Yahoo industry mapped to the sector taxonomy (263); out_of_scope: issuer not in the universe yet (13) |

Listed but outside the register definition (status `excluded`): US: OTC quotation only 1,944, investment company 360, no listed common equity 133, new registrant: no periodic report yet 65, no 10-K / 10-Q / 20-F / 40-F in the EDGAR index since 2024-11-08 16.

Sources (W2). Used: SEC company_tickers_exchange.json + EDGAR full-index master.gz (register), companyfacts bulk zip (statements of every register CIK) and Financial Statement Data Sets 2009q1 -> (20-F / 40-F filers); ESMA FIRDS FULINS_E weekly files + ESMA regulated-market register (upreg MIR) + ISO 10383 MIC list (EU register); FCA FIRDS FULINS_E (data.fca.org.uk artefacts; UK Official List equity on XLON + AIM on AIMX); GLEIF lei-records API (names, legal country, company numbers), OpenFIGI mapping (ISIN -> home listing, cached) and Yahoo 3-month downloads (price-source check); filings.xbrl.org ESEF / UKSEF xBRL-JSON (EU / UK annual reports), FCA NSM tagged AFRs, the national OAM adapters (DILA, AFM, FSMA, eMarket, Nasdaq Nordic, Oslo, CNMV); Companies House Accounts Data Product (free daily / monthly iXBRL zips, read with HTTP Range requests: central directory + the target members only); Yahoo statements (yfinance, annual + quarterly) as the documented, non-official fallback.

Rejected / not used: FCA Official List web search (marketsecurities.fca.org.uk): robots.txt disallows all bots; LSE instrument / AIM lists (londonstockexchange.com reports API): answers 403 to non-browser clients (WAF) - not bypassed; FCA FIRDS covers the same issuers; Companies House REST API: needs COMPANIES_HOUSE_APIKEY (not needed: the bulk product is keyless); its PDF accounts are not parsed; FCA NSM market-wide search: WAF 403 on paging (per-LEI queries only); Zagreb, Ljubljana, Sofia, Malta, Cyprus, Luxembourg, Bratislava prices: no free daily source (Yahoo has no market); those issuers get reference data + statements, prices reason no_source.

<!-- regulator-coverage:end -->

## UI: every empty value explains itself

* `frontend/src/components/ui/missing.tsx`: `<Missing field region reason>` (an underlined "—" with a tooltip: label,
  reason text, fix / owner; `data-reason` = the code), `<Avail value field>` (value or the explained dash),
  `<AvailabilityScope securityId>` (loads `GET /companies/{id}/availability` once for Company 360) and
  `useMissingReason()`. Pure helpers in `frontend/src/lib/availability.ts` (resolution order: explicit reason ->
  the security's own reason -> field reason in its region -> field main reason -> generic).
* Wired into: Company 360 header (market cap, EV, beta) and Overview key ratios; Earnings tab (expectations,
  actuals, surprises, reaction windows "not complete yet, ends ~date"); Ownership tab (13F / SI / score stats and
  empty panels); Technicals factor card (no silent blank: the card says why); Screener (P/E, EV/EBIT, FCF yld, ROIC,
  Rev g, ND/EBITDA, Div yld, Mkt cap, E[xs], grade, macro betas); Smart Money tables (13F, SI, FTD, score, EU short
  columns); Earnings calendar ("Not published yet: reports on 2026-11-05", "release date passed; results not
  ingested yet"); Macro tiles (discontinued / empty / stale series); Cross-asset cockpit (no CFTC market, no carry
  definition, too little history).
* API (contract 1.42.0, additive): `GET /meta/field-availability`, `GET /companies/{id}/availability`.

<!-- generated: app.qa.completeness -->

# Data completeness (field availability)

Generated by `python -m app.qa.completeness --write` on 2026-10-31T07:20:07+00:00 from the live API (snapshot `SI20261031T0619213D3X`, data as of 2026-10-30). Machine-readable twin: `contracts/field_availability.yaml` (used by the UI tooltips, `GET /api/v1/meta/field-availability`, `GET /api/v1/companies/{id}/availability` and the startup sync engine).

**Method.** Every BQL field (`contracts/bql_fields.yaml`) is requested for `members('full')` on `POST /api/v1/bql` (compute-only, sequential, 25 fields per request). Coverage = share of securities with a non-null value, per universe (full / mvp_us / mvp_global) and per region (universe full). A region below 95 % gets a reason from the rule table in `app/qa/completeness.py`. Fields with the same gaps share one row.

## Reason codes

| Code | Sync code | Meaning |
|---|---|---|
| `fresh` | `fresh` | Up to date |
| `due` | `due` | Pending data refresh: the source has newer data |
| `not_yet_published` | `not_yet_published` | Not published yet |
| `source_unavailable` | `source_unavailable` | Source temporarily unavailable; retrying |
| `no_source` | `no_source` | No free source for this market |
| `needs_key` | `needs_key` | Needs an API key |
| `failed` | `failed` | The last update failed; it is retried automatically |
| `mapping_gap` | `failed` | Identifier not matched to the source record |
| `pipeline_bug` | `failed` | Known pipeline gap: fix pending |
| `stale` | `due` | Pending data refresh |
| `not_applicable` | `fresh` | Not applicable to this company |
| `out_of_scope` | `no_source` | Not computed for this company's universe yet |
| `suppressed` | `fresh` | Value withheld: outside plausibility bounds |
| `insufficient_data` | `no_source` | Not enough inputs to compute this score |

## Reason distribution (field x region gaps)

| Reason | Gaps |
|---|---|
| `not_applicable` | 2030 |
| `out_of_scope` | 892 |
| `no_source` | 862 |
| `insufficient_data` | 660 |
| `needs_key` | 312 |
| `source_unavailable` | 281 |
| `stale` | 277 |
| `suppressed` | 165 |
| `failed` | 124 |
| `not_yet_published` | 27 |
| `mapping_gap` | 16 |

Universe sizes: {'full': 23828, 'mvp_us': 500, 'mvp_global': 271}; securities per region (full): {'CN': 5224, 'KR': 2655, 'HK': 2461, 'APAC': 1994, 'JP': 3709, 'EU': 1385, 'US': 5604, 'UK': 354, 'LATAM': 438, 'CA': 4}.

## Identity & classification

21 fields; median coverage (universe full) 96.1 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `lei` | 7.8 | 0.4 | 73.4 | US 0 → `no_source` (r29); LATAM 11 → `no_source` (r29); JP 1 → `no_source` (r29); KR 0 → `no_source` (r29); CN 0 → `no_source` (r29); HK 1 → `no_source` (r29); APAC 0 → `no_source` (r29) |
| `employees` | 11.2 | 97.6 | 93.7 | US 14 → `out_of_scope` (r34); LATAM 14 → `out_of_scope` (r34); JP 1 → `out_of_scope` (r34); KR 1 → `out_of_scope` (r34); CN 0 → `out_of_scope` (r34); HK 1 → `out_of_scope` (r34); APAC 1 → `out_of_scope` (r34) |
| `security_type` | 16.8 | 99.6 | 100.0 | US 48 → `not_applicable` (default); LATAM 17 → `not_applicable` (default); UK 37 → `not_applicable` (default); EU 69 → `not_applicable` (default); JP 1 → `not_applicable` (default); KR 1 → `not_applicable` (default); CN 0 → `not_applicable` (default); HK 1 → `not_applicable` (default); APAC 1 → `not_applicable` (default) |
| `figi` | 19.0 | 93.4 | 0.7 | US 80 → `out_of_scope` (r30); LATAM 6 → `out_of_scope` (r30); UK 4 → `out_of_scope` (r30); EU 1 → `out_of_scope` (r30); JP 0 → `out_of_scope` (r30); KR 0 → `out_of_scope` (r30); CN 0 → `out_of_scope` (r30); HK 0 → `out_of_scope` (r30); APAC 0 → `out_of_scope` (r30) |
| `cik`, `cusip` | 23.7–24.2 | 99.8–100.0 | 5.9–31.7 | LATAM 6–12 → `not_applicable` (r28); UK 5–9 → `not_applicable` (r28); EU 2–2 → `not_applicable` (r28); JP 0–0 → `not_applicable` (r28); KR 0–0 → `not_applicable` (r28); CN 0 → `not_applicable` (r28); HK 0–0 → `not_applicable` (r28); APAC 0–0 → `not_applicable` (r28) |
| `last_filing_date` | 40.0 | 100.0 | 88.9 | LATAM 17 → `not_applicable` (default); UK 84 → `not_applicable` (default); EU 74 → `not_applicable` (default); JP 68 → `not_applicable` (default); KR 1 → `not_applicable` (default); CN 0 → `not_applicable` (default); HK 1 → `not_applicable` (default); APAC 0 → `not_applicable` (default) |
| `name` | 57.1 | 93.6 | 62.4 | US 81 → `not_applicable` (default); LATAM 93 → `not_applicable` (default); JP 0 → `not_applicable` (default); CN 0 → `not_applicable` (default); APAC 93 → `not_applicable` (default) |
| `country`, `region` | 82.0 | 99.6 | 100.0 | US 68 → `not_applicable` (default); LATAM 94 → `not_applicable` (default); HK 1 → `not_applicable` (default) |
| `isin` | 96.1 | 100.0 | 100.0 | APAC 55 → `mapping_gap` (r27) |
| `is_primary` | 99.7 | 100.0 | 100.0 | LATAM 94 → `not_applicable` (default) |
| `ticker`, `issuer`, `mic`, `exchange`, `sector`, `subsector`, `currency`, `size_bucket`, `reporting_currency` | 99.9–100.0 | 100.0 | 100.0 | none |

## Prices & returns

20 fields; median coverage (universe full) 99.7 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `turnover_usd`, `px_date` | 0.0 | 0.0 | 0.0 | US 0 → `stale` (r40); LATAM 0 → `stale` (r40); UK 0 → `stale` (r40); EU 0 → `stale` (r40); JP 0 → `stale` (r40); KR 0 → `stale` (r40); CN 0 → `stale` (r40); HK 0 → `stale` (r40); APAC 0 → `stale` (r40) |
| `chg_pct_12m`, `mom_12_1` | 96.0 | 98.2 | 100.0 | US 92 → `stale` (r40); LATAM 92 → `stale` (r40); HK 93 → `stale` (r40) |
| `chg_pct_ytd` | 97.0 | 98.4 | 100.0 | US 93 → `stale` (r40); HK 95 → `stale` (r40) |
| `px_high_adj`, `px_low_adj`, `px_last_usd`, `chg_pct_1d`, `chg_pct_1w`, `chg_pct_1m`, `chg_pct_3m`, `chg_pct_6m`, `mom_6_1` | 98.0–99.9 | 99.0–100.0 | 100.0 | none |
| `px`, `px_last`, `px_open`, `px_high`, `px_low`, `px_volume` | 99.7 | 100.0 | 100.0 | LATAM 94 → `stale` (r40) |

## Technicals

211 fields; median coverage (universe full) 97.7 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `tech.composite`, `tech.composite_pct` | 0.0 | 0.0 | 0.0 | US 0 → `suppressed` (r01); LATAM 0 → `suppressed` (r01); UK 0 → `suppressed` (r01); EU 0 → `suppressed` (r01); JP 0 → `suppressed` (r01); KR 0 → `suppressed` (r01); CN 0 → `suppressed` (r01); HK 0 → `suppressed` (r01); APAC 0 → `suppressed` (r01) |
| `cf.earn_days_ahead` | 21.9 | 37.4 | 37.6 | US 24 → `not_yet_published` (r04); LATAM 31 → `not_yet_published` (r04); UK 22 → `not_yet_published` (r04); EU 34 → `not_yet_published` (r04); JP 66 → `not_yet_published` (r04); KR 6 → `not_yet_published` (r04); CN 1 → `not_yet_published` (r04); HK 5 → `not_yet_published` (r04); APAC 21 → `not_yet_published` (r04) |
| `bo.as_of_date`, `bo.state`, `bo.direction`, `bo.lookback`, `bo.event_date`, `bo.sessions_since`, `bo.level`, `bo.target_20`, `bo.last_close`, `bo.confirmed` +7 more | 45.8 | 92.8 | 88.9 | US 56 → `not_applicable` (r02); LATAM 38 → `not_applicable` (r02); UK 62 → `not_applicable` (r02); EU 60 → `not_applicable` (r02); JP 34 → `not_applicable` (r02); KR 26 → `not_applicable` (r02); CN 70 → `not_applicable` (r02); HK 14 → `not_applicable` (r02); APAC 30 → `not_applicable` (r02) |
| `tech.as_of_date`, `tech.sector_id`, `tech.hi52`, `tech.strev`, `tech.maxret`, `tech.ivol`, `tech.indmom`, `tech.hi52_pct`, `tech.strev_pct`, `tech.maxret_pct` +7 more | 65.5–75.3 | 99.0–99.8 | 87.5–98.2 | US 64–86 → `not_applicable` (r38); LATAM 49–54 → `not_applicable` (r38); JP 62–63 → `not_applicable` (r38); KR 55–57 → `not_applicable` (r38); HK 0–38 → `not_applicable` (r38); APAC 59–61 → `not_applicable` (r38) |
| `tech.mom`, `tech.rmom`, `tech.mom_pct`, `tech.rmom_pct` | 70.0–72.7 | 97.0–98.2 | 96.7–98.2 | US 75–81 → `not_applicable` (r38); LATAM 52–53 → `not_applicable` (r38); EU 92–94 → `not_applicable` (r38); JP 61–62 → `not_applicable` (r38); KR 52–55 → `not_applicable` (r38); HK 30–33 → `not_applicable` (r38); APAC 59–60 → `not_applicable` (r38) |
| `cf.touch_20d`, `cf.band_5d_90`, `cf.as_of_date`, `cf.pool_key`, `cf.sector_id`, `cf.filter`, `cf.radius_mode`, `cf.last_close`, `cf.sigma_d`, `cf.earn_jump_var` +46 more | 84.4 | 98.2 | 100.0 | US 46 → `failed` (r36); LATAM 69 → `failed` (r36); UK 95 → `stale` (r39); HK 92 → `stale` (r39) |
| `setup.target2` | 84.4 | 91.6 | 90.4 | US 84 → `not_applicable` (r03); LATAM 80 → `not_applicable` (r03); UK 74 → `not_applicable` (r03); EU 83 → `not_applicable` (r03); JP 93 → `not_applicable` (r03); KR 79 → `not_applicable` (r03); CN 82 → `not_applicable` (r03); HK 81 → `not_applicable` (r03); APAC 92 → `not_applicable` (r03) |
| `tp.vol_regime` | 92.7 | 99.8 | 64.2 | LATAM 85 → `stale` (r39); UK 20 → `stale` (r37); EU 18 → `stale` (r37) |
| `trend_200d`, `above_200d`, `setup.sma200` | 96.7–96.9 | 98.4 | 100.0 | US 93 → `stale` (r39); LATAM 89–94 → `stale` (r39); HK 95–95 → `stale` (r39) |
| `zone.reward_risk` | 97.2 | 99.2 | 99.3 | LATAM 90 → `stale` (r39); UK 92 → `stale` (r39); HK 94 → `stale` (r39) |
| `setup.support_zone_id`, `setup.support_low`, `setup.support_high`, `setup.dist_support_atr`, `setup.dist_support_pct` | 97.7 | 99.8 | 100.0 | LATAM 91 → `stale` (r39); HK 94 → `stale` (r39) |
| `beta_1y`, `zone.side`, `zone.center`, `zone.distance_atr`, `tp.as_of_date`, `tp.sector_id`, `tp.technical_score`, `tp.technical_score_pct`, `tp.trend_pct`, `tp.structure_pct` +76 more | 98.1–99.0 | 99.0–99.8 | 100.0 | LATAM 92–93 → `stale` (r39) |
| `setup.resistance_zone_id`, `setup.resistance_low`, `setup.resistance_high`, `setup.dist_resistance_atr`, `setup.dist_resistance_pct` | 98.5 | 99.2 | 99.3 | LATAM 92 → `stale` (r39); UK 93 → `stale` (r39) |
| `hi52`, `lo52`, `px_to_hi52`, `sma50`, `sma200`, `above_50d`, `new_hi52`, `new_lo52`, `vol_1y`, `atr14` +2 more | 99.1–99.9 | 100.0 | 100.0 | none |

## Fundamentals

165 fields; median coverage (universe full) 14.9 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `fa.selling_marketing`, `fa.general_admin`, `fa.rnd`, `fa.interest_income`, `fa.equity_method_income`, `fa.ordinary_income`, `fa.net_income_discontinued`, `fa.preferred_dividends`, `fa.short_term_investments`, `fa.long_term_investments` +34 more | 0.3–11.4 | 0.0–64.8 | 0.0–62.7 | US 0–29 → `not_applicable` (r46); LATAM 0–16 → `not_applicable` (r46); UK 0–68 → `not_applicable` (r46); EU 0–54 → `not_applicable` (r46); JP 0–21 → `not_applicable` (r46); KR 0–1 → `needs_key` (r42); CN 0–0 → `source_unavailable` (r43); HK 0–1 → `no_source` (r44); APAC 0–0 → `no_source` (r45) |
| `gross_margin`, `operating_margin`, `ebitda_margin`, `net_margin`, `fcf_margin`, `roic`, `croic`, `gross_profitability`, `fcf_conversion`, `gm_stdev_5y` +81 more | 2.3–19.2 | 22.6–98.8 | 8.5–100.0 | US 8–47 → `out_of_scope` (r48); LATAM 0–17 → `out_of_scope` (r48); UK 1–95 → `out_of_scope` (r48); EU 5–95 → `out_of_scope` (r48); JP 0–22 → `out_of_scope` (r48); KR 0–1 → `needs_key` (r42); CN 0–0 → `source_unavailable` (r43); HK 0–1 → `no_source` (r44); APAC 0–1 → `no_source` (r45) |
| `fa.shares_outstanding` | 16.9 | 99.6 | 100.0 | US 48 → `out_of_scope` (r35); LATAM 17 → `out_of_scope` (r35); UK 37 → `out_of_scope` (r35); EU 70 → `out_of_scope` (r35); JP 1 → `out_of_scope` (r35); KR 1 → `out_of_scope` (r35); CN 0 → `out_of_scope` (r35); HK 1 → `out_of_scope` (r35); APAC 1 → `out_of_scope` (r35) |
| `fa.interest_expense`, `fa.ppe_net` | 17.9–21.2 | 90.8–95.4 | 98.5–100.0 | US 41–44 → `out_of_scope` (r48); LATAM 17 → `out_of_scope` (r48); EU 91–92 → `out_of_scope` (r48); JP 5–22 → `out_of_scope` (r48); KR 1 → `needs_key` (r42); CN 0 → `source_unavailable` (r43); HK 1 → `no_source` (r44); APAC 1 → `no_source` (r45) |
| `roa`, `ebit_growth_yoy`, `liabilities_to_assets`, `cash_to_assets`, `fa.total_assets`, `fa.total_liabilities`, `fa.operating_working_capital` | 18.2–22.7 | 86.6–99.6 | 99.6–100.0 | US 38–48 → `out_of_scope` (r48); LATAM 17 → `out_of_scope` (r48); UK 92–95 → `out_of_scope` (r48); JP 5–22 → `out_of_scope` (r48); KR 1 → `needs_key` (r42); CN 0 → `source_unavailable` (r43); HK 1 → `no_source` (r44); APAC 1 → `no_source` (r45) |
| `tax_rate_norm`, `nopat`, `roe`, `cfo_to_net_income`, `interest_coverage`, `cash_conversion_cycle`, `fa.pretax_income`, `fa.income_tax`, `fa.net_income_continuing`, `fa.net_income` +10 more | 18.7–22.7 | 96.4–99.6 | 99.6–100.0 | US 43–48 → `out_of_scope` (r48); LATAM 17 → `out_of_scope` (r48); JP 5–22 → `out_of_scope` (r48); KR 1 → `needs_key` (r42); CN 0 → `source_unavailable` (r43); HK 1 → `no_source` (r44); APAC 1 → `no_source` (r45) |

## Valuation

62 fields; median coverage (universe full) 16.5 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `pe_ttm`, `pe_fwd`, `pb`, `ps_ttm`, `ev_sales`, `ev_ebitda`, `ev_ebit`, `fcf_yield`, `div_yld`, `earnings_yield` +24 more | 9.8–17.6 | 63.8–99.6 | 75.6–100.0 | US 26–47 → `out_of_scope` (r48); LATAM 8–17 → `out_of_scope` (r48); UK 19–79 → `out_of_scope` (r48); EU 48–87 → `out_of_scope` (r48); JP 1–5 → `out_of_scope` (r48); KR 0–1 → `needs_key` (r42); CN 0–0 → `source_unavailable` (r43); HK 0–1 → `no_source` (r44); APAC 0–1 → `no_source` (r45) |
| `mkt_cap_usd`, `ev_usd`, `shares_out` | 15.6–16.9 | 98.8–99.6 | 99.6–100.0 | US 45–48 → `out_of_scope` (r35); LATAM 10–17 → `out_of_scope` (r35); UK 29–37 → `out_of_scope` (r35); EU 67–70 → `out_of_scope` (r35); JP 1–1 → `out_of_scope` (r35); KR 1–1 → `out_of_scope` (r35); CN 0 → `out_of_scope` (r35); HK 1–1 → `out_of_scope` (r35); APAC 0–1 → `out_of_scope` (r35) |
| `coc.interest_coverage` | 17.2 | 81.2 | 98.5 | US 38 → `out_of_scope` (r48); LATAM 17 → `out_of_scope` (r48); EU 93 → `out_of_scope` (r48); JP 6 → `out_of_scope` (r48); KR 1 → `needs_key` (r42); CN 0 → `source_unavailable` (r43); HK 1 → `no_source` (r44); APAC 1 → `no_source` (r45) |
| `val.fundamentals_knowledge_date` | 19.0 | 98.8 | 99.6 | US 45 → `out_of_scope` (r48); LATAM 10 → `out_of_scope` (r48); UK 90 → `out_of_scope` (r48); JP 6 → `out_of_scope` (r48); KR 1 → `needs_key` (r42); CN 0 → `source_unavailable` (r43); HK 1 → `no_source` (r44); APAC 0 → `no_source` (r45) |
| `coc.as_of_date`, `coc.currency`, `coc.rf`, `coc.rf_source`, `coc.erp`, `coc.crp`, `coc.beta_raw`, `coc.beta`, `coc.beta_source`, `coc.n_weeks` +11 more | 19.7–20.0 | 98.2–98.8 | 100.0 | US 47–47 → `out_of_scope` (r48); LATAM 17 → `out_of_scope` (r48); JP 6–6 → `out_of_scope` (r48); KR 1 → `needs_key` (r42); CN 0 → `source_unavailable` (r43); HK 1 → `no_source` (r44); APAC 1 → `no_source` (r45) |
| `val.date`, `val.close_usd` | 86.7 | 100.0 | 100.0 | US 47 → `out_of_scope` (r48); LATAM 70 → `out_of_scope` (r48) |

## Scores & factors

93 fields; median coverage (universe full) 18.3 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `composite`, `grade`, `rank_global`, `f.quality`, `f.quality.rank_sector`, `f.quality.chg_1m`, `f.profitability`, `f.profitability.rank_sector`, `f.profitability.chg_1m`, `f.growth` +45 more | 3.2–18.6 | 96.8–100.0 | 95.6–100.0 | US 9–43 → `insufficient_data` (r47); LATAM 10–17 → `insufficient_data` (r47); UK 8–92 → `insufficient_data` (r47); EU 6–95 → `insufficient_data` (r47); JP 1–6 → `insufficient_data` (r47); KR 1–1 → `needs_key` (r42); CN 0 → `source_unavailable` (r43); HK 1–1 → `no_source` (r44); APAC 0–1 → `no_source` (r45) |
| `ml.extrapolated_1m`, `ml.extrapolated_3m`, `ml.extrapolated_6m`, `ml.rank_pct_1m`, `ml.decile_1m`, `ml.score_1m`, `ml.exp_excess_1m`, `ml.rank_pct_3m`, `ml.decile_3m`, `ml.score_3m` +5 more | 3.3 | 100.0 | 100.0 | US 9 → `suppressed` (r41); LATAM 10 → `suppressed` (r41); UK 8 → `suppressed` (r41); EU 6 → `suppressed` (r41); JP 1 → `suppressed` (r41); KR 1 → `suppressed` (r41); CN 0 → `suppressed` (r41); HK 1 → `suppressed` (r41); APAC 0 → `suppressed` (r41) |
| `moat`, `moat_score`, `f.moat`, `f.moat.rank_sector`, `f.moat.rank_sector_full` | 18.8–19.9 | 98.6–98.8 | 100.0 | US 43–47 → `insufficient_data` (r47); LATAM 17 → `insufficient_data` (r47); JP 5–6 → `insufficient_data` (r47); KR 1 → `needs_key` (r42); CN 0 → `source_unavailable` (r43); HK 1 → `no_source` (r44); APAC 1 → `no_source` (r45) |
| `score_missing` | 96.3 | 0.0 | 0.0 | US 91 → `insufficient_data` (r47); LATAM 83 → `insufficient_data` (r47); UK 88 → `insufficient_data` (r47); EU 92 → `insufficient_data` (r47) |
| `ml.status_1m`, `ml.status_3m`, `ml.status_6m` | 96.7 | 0.0 | 0.0 | US 91 → `suppressed` (r41); LATAM 90 → `suppressed` (r41); UK 92 → `suppressed` (r41); EU 94 → `suppressed` (r41) |
| `f.momentum`, `f.momentum.rank_sector`, `f.momentum.rank_sector_full` | 97.8 | 98.8 | 99.6 | LATAM 87 → `insufficient_data` (r47) |
| `dq_score`, `f.insider`, `f.insider.rank_sector`, `f.sentiment`, `f.sentiment.rank_sector`, `f.macro`, `f.macro.rank_sector`, `f.insider.rank_sector_full`, `f.sentiment.rank_sector_full`, `f.macro.rank_sector_full` +1 more | 100.0 | 100.0 | 100.0 | none |

## Sector KPIs

54 fields; median coverage (universe full) 0.7 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `kpi.rule_of_40`, `kpi.fcf_margin_after_sbc`, `kpi.deferred_revenue_growth`, `kpi.rpo_growth`, `kpi.opex_leverage`, `kpi.rnd_intensity_3y`, `kpi.inventory_days_change`, `kpi.capex_intensity`, `kpi.revenue_per_employee`, `kpi.dividend_fcf_coverage` +44 more | 0.0–1.9 | 0.0–10.6 | 0.0–15.5 | US 0–5 → `not_applicable` (r07); LATAM 0–4 → `not_applicable` (r07); UK 0–12 → `not_applicable` (r07); EU 0–10 → `not_applicable` (r07); JP 0–1 → `not_applicable` (r07); KR 0–0 → `not_applicable` (r07); CN 0–0 → `not_applicable` (r07); HK 0–0 → `not_applicable` (r07); APAC 0–0 → `not_applicable` (r07) |

## Earnings

65 fields; median coverage (universe full) 10.5 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `pead.outcome_car_p2_p20`, `pead.outcome_car_p2_p60` | 3.1–7.9 | 29.4–57.8 | 9.2–28.0 | US 13–32 → `not_yet_published` (r05); LATAM 2–5 → `not_yet_published` (r05); UK 2–3 → `not_yet_published` (r05); EU 0–2 → `not_yet_published` (r05); JP 0 → `not_yet_published` (r05); KR 0–1 → `not_yet_published` (r05); CN 0–0 → `not_yet_published` (r05); HK 0 → `not_yet_published` (r05); APAC 0–0 → `not_yet_published` (r05) |
| `eps_surprise`, `eps_surprise_z`, `rev_surprise`, `earn.eps_model`, `earn.eps_expected`, `earn.eps_lo80`, `earn.eps_hi80`, `earn.eps_actual`, `earn.eps_surprise`, `earn.eps_surprise_pct` +21 more | 7.3–10.5 | 72.8–95.6 | 22.5–38.0 | US 28–42 → `insufficient_data` (r23); LATAM 7–14 → `insufficient_data` (r23); UK 1–4 → `no_source` (r21); EU 2–2 → `no_source` (r21); JP 0–0 → `insufficient_data` (r19); KR 0–1 → `needs_key` (r20); CN 0–0 → `no_source` (r22); HK 0–0 → `no_source` (r22); APAC 0–0 → `no_source` (r22) |
| `pead.decile` | 10.0 | 95.6 | 0.4 | US 42 → `not_applicable` (r06); LATAM 0 → `not_applicable` (r06); UK 0 → `not_applicable` (r06); EU 2 → `not_applicable` (r06); JP 0 → `not_applicable` (r06); KR 0 → `not_applicable` (r06); CN 0 → `not_applicable` (r06); HK 0 → `not_applicable` (r06); APAC 0 → `not_applicable` (r06) |
| `earn.fiscal_year`, `earn.fiscal_period`, `earn.report_filing_date`, `earn.provider_date`, `earn.drift_continuation`, `earn.reaction_complete` | 20.7–48.4 | 42.2–97.4 | 55.7–86.3 | US 45–87 → `insufficient_data` (r25); LATAM 15–60 → `insufficient_data` (r25); UK 14–81 → `insufficient_data` (r25); EU 35–82 → `insufficient_data` (r25); JP 1–42 → `insufficient_data` (r25); KR 1–25 → `insufficient_data` (r25); CN 0–29 → `insufficient_data` (r25); HK 0–12 → `insufficient_data` (r25); APAC 0–76 → `insufficient_data` (r25) |
| `earn.trade_reaction_date`, `earn.benchmark_id`, `earn.beta`, `earn.sigma_daily`, `earn.ret_m1_p1`, `earn.car_m1_p1`, `earn.car_0_p5`, `earn.car_p1_p20`, `earn.bhar_m1_p1`, `earn.bhar_0_p5` +6 more | 21.9–48.4 | 42.2–91.0 | 35.4–86.3 | US 44–85 → `insufficient_data` (r24); LATAM 28–56 → `insufficient_data` (r24); UK 34–81 → `insufficient_data` (r24); EU 32–82 → `insufficient_data` (r24); JP 21–42 → `insufficient_data` (r24); KR 4–18 → `insufficient_data` (r24); CN 11–27 → `insufficient_data` (r24); HK 4–10 → `insufficient_data` (r24); APAC 24–69 → `insufficient_data` (r24) |
| `earn.country`, `earn.period_end`, `earn.period_inferred`, `earn.release_date`, `earn.session`, `earn.reaction_date`, `earn.date_source`, `earn.form_type`, `earn.knowledge_date` | 30.4–55.7 | 99.2–99.8 | 41.7–98.5 | LATAM 11–61 → `insufficient_data` (r25); UK 72–86 → `insufficient_data` (r25); EU 31–90 → `insufficient_data` (r25); JP 30–53 → `insufficient_data` (r25); KR 0–25 → `insufficient_data` (r25); CN 0–29 → `insufficient_data` (r25); HK 0–12 → `insufficient_data` (r25); APAC 0–76 → `insufficient_data` (r25) |

## Smart money

95 fields; median coverage (universe full) 22.8 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `si.pct_float`, `sm.smart_flow`, `sm.si_pct_out`, `sm.z_smart_flow`, `sm.z_si_level`, `sm.g_smart`, `si.shares_out`, `si.shares_out_source`, `si.market_cap_usd`, `si.si_pct_out` +12 more | 2.2–11.2 | 88.4–99.2 | 3.7–29.2 | US 9–47 → `out_of_scope` (r16); LATAM 0–6 → `not_applicable` (r18); UK 0–4 → `not_applicable` (r18); EU 0–1 → `not_applicable` (r18); JP 0–0 → `not_applicable` (r18); KR 0–0 → `not_applicable` (r18); CN 0 → `not_applicable` (r18); HK 0–0 → `not_applicable` (r18); APAC 0–0 → `not_applicable` (r18) |
| `sm.eu_short_pct`, `sm.eu_short_chg_30d`, `sm.eu_short_holders`, `sm.z_eu_level`, `sm.z_eu_chg`, `sm.g_eu_short` | 3.0 | 0.0 | 15.1 | US 0 → `not_applicable` (r15); LATAM 0 → `not_applicable` (r15); UK 84 → `not_applicable` (r14); EU 29 → `no_source` (r13); JP 0 → `not_applicable` (r15); KR 0 → `not_applicable` (r15); CN 0 → `not_applicable` (r15); HK 0 → `not_applicable` (r15); APAC 0 → `not_applicable` (r15) |
| `sm.sector_id`, `sm.inst_flow`, `sm.z_inst_flow`, `si.ftd_max_shares`, `si.ftd_z`, `si.ftd_knowledge_date`, `own.n_holders_prev`, `own.shares_held_prev`, `own.shares_held_chg_pct`, `own.hf_share_of_inst` +5 more | 10.1–22.0 | 89.0–99.6 | 5.9–48.0 | US 42–92 → `mapping_gap` (r17); LATAM 1–13 → `not_applicable` (r18); UK 1–92 → `not_applicable` (r18); EU 1–32 → `not_applicable` (r18); JP 0–0 → `not_applicable` (r18); KR 0–0 → `not_applicable` (r18); CN 0 → `not_applicable` (r18); HK 0–1 → `not_applicable` (r18); APAC 0–0 → `not_applicable` (r18) |
| `sm.insider_signal`, `sm.z_insider`, `sm.g_insider` | 11.4 | 81.2 | 15.9 | US 47 → `not_applicable` (r10); LATAM 3 → `no_source` (r12); UK 14 → `not_applicable` (r10); EU 0 → `out_of_scope` (r11); JP 0 → `no_source` (r12); KR 0 → `no_source` (r12); CN 0 → `no_source` (r12); HK 1 → `no_source` (r12); APAC 0 → `no_source` (r12) |
| `si.days`, `sm.as_of_date`, `sm.knowledge_date`, `sm.inst_period_end`, `sm.inst_breadth`, `sm.si_chg_pct`, `sm.si_settlement_date`, `sm.z_inst_breadth`, `sm.z_si_chg`, `sm.g_inst` +39 more | 22.8–27.0 | 99.2–99.8 | 5.9–48.0 | LATAM 6–13 → `not_applicable` (r18); UK 5–92 → `not_applicable` (r18); EU 2–32 → `not_applicable` (r18); JP 0–0 → `not_applicable` (r18); KR 0–0 → `not_applicable` (r18); CN 0 → `not_applicable` (r18); HK 0–1 → `not_applicable` (r18); APAC 0–0 → `not_applicable` (r18) |

## Insiders

19 fields; median coverage (universe full) 14.1 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `insider_signal`, `ins.cluster_buy_flag`, `ins.c_suite_buy_flag`, `ins.net_buy_90d_usd`, `ins.anomaly`, `ins.as_of_date`, `ins.net_buy_value_usd_30d`, `ins.net_buy_value_usd_90d`, `ins.net_buy_mcap_bps_90d`, `ins.n_buyers_90d` +9 more | 7.3–14.1 | 80.2–96.6 | 13.7–18.1 | US 30–58 → `not_applicable` (r10); LATAM 3–4 → `no_source` (r12); UK 5–14 → `not_applicable` (r10); EU 0 → `out_of_scope` (r11); JP 0 → `no_source` (r12); KR 0 → `no_source` (r12); CN 0 → `no_source` (r12); HK 0–1 → `no_source` (r12); APAC 0–0 → `no_source` (r12) |

## News & sentiment

22 fields; median coverage (universe full) 20.7 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `sent.lazy_prices_sim`, `sent.lazy_prices_pct`, `sent.lazy_prices_large_change` | 2.0 | 95.0 | 0.0 | US 9 → `not_applicable` (r09); LATAM 0 → `not_applicable` (r08); UK 0 → `not_applicable` (r08); EU 0 → `not_applicable` (r08); JP 0 → `not_applicable` (r08); KR 0 → `not_applicable` (r08); CN 0 → `not_applicable` (r08); HK 0 → `not_applicable` (r08); APAC 0 → `not_applicable` (r08) |
| `sentiment`, `news_count_30d`, `attention_z`, `sent.date`, `sent.news_count`, `sent.news_count_z`, `sent.sentiment_mean`, `sent.sentiment_ewm_7d`, `sent.sentiment_ewm_30d`, `sent.gdelt_tone_ewm_7d` +9 more | 3.0–23.8 | 54.2–100.0 | 4.8–73.8 | US 12–79 → `not_applicable` (r09); LATAM 1–10 → `not_applicable` (r09); UK 1–60 → `not_applicable` (r09); EU 0–24 → `not_applicable` (r09); JP 0–12 → `not_applicable` (r09); KR 0–0 → `not_applicable` (r09); CN 0–0 → `not_applicable` (r09); HK 0–5 → `not_applicable` (r09); APAC 0–5 → `not_applicable` (r09) |

## Macro exposures

6 fields; median coverage (universe full) 84.3 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `macro.beta_rates`, `macro.beta_usd`, `macro.beta_oil`, `macro.beta_credit`, `macro.beta_growth`, `macro.rates_down_100bp` | 84.3 | 98.0 | 98.9–100.0 | US 45 → `failed` (r36); LATAM 70–70 → `failed` (r36); UK 94 → `stale` (r49); HK 92 → `stale` (r49) |

## Other

4 fields; median coverage (universe full) 100.0 %.

| Fields | Full % | mvp_us % | mvp_global % | Gaps: region % → reason (rule) |
|---|---|---|---|---|
| `u_mvp_us`, `u_mvp_global`, `u_full`, `u_watch` | 100.0 | 100.0 | 100.0 | none |

## Rules (reason, note, fix / owner)

| Rule | Reason | Note | Fix / owner |
|---|---|---|---|
| r01 | `suppressed` | The technical-factor composite failed its pre-registered validation gate and is withheld (docs/TECHNICALS_V3_VALIDATION.md) | by design |
| r02 | `not_applicable` | No breakout in the last 20 sessions | by design |
| r03 | `not_applicable` | The setup has no second target level | by design |
| r04 | `not_yet_published` | No upcoming earnings date announced | earnings calendar (JPX / provider dates) |
| r05 | `not_yet_published` | The post-earnings outcome window has not ended yet | by design |
| r06 | `not_applicable` | PEAD deciles need enough same-region releases in the quarter | by design |
| r07 | `not_applicable` | Sector KPI: computed only for companies of the KPI set's sectors and when the line items are reported | by design |
| r08 | `not_applicable` | 10-K / 10-Q text similarity: SEC filers only | by design |
| r09 | `not_applicable` | No news items linked to this company in the window | news linker (events-v2 / news-live) |
| r10 | `not_applicable` | No insider (Form 4 / PDMR) transactions in the signal window | by design |
| r11 | `out_of_scope` | EU PDMR notices (AFM, BaFin) are stored but not scored by the insider model | analytics-insider: score EU PDMR |
| r12 | `no_source` | No free structured insider-transaction feed for this market | - |
| r13 | `no_source` | DE (BaFin) and IT (CONSOB) registers are not machine-readable (robots / captcha); positions below 0.5 % are not published | user decision 2026-10-19: skipped |
| r14 | `not_applicable` | No disclosed net short position >= 0.5 % | by design |
| r15 | `not_applicable` | EU / UK short-selling disclosure regime only | by design |
| r16 | `out_of_scope` | Needs shares outstanding, which is loaded only for issuers with financial statements | 2026-10-27: ratios_kpis + valuation_daily default universe=full (every issuer with loaded statements); statements for the remaining US small caps need sec_xbrl_bulk all_ciks (ref-identity / ingest-us) |
| r17 | `mapping_gap` | FINRA / 13F / FTD row not matched to this listing (symbol or CUSIP) | 2026-10-27: compact class-share, ETF, OpenFIGI and FTD-history symbol resolution; ADR rows kept on the US listing (app.normalize.us_symbols) |
| r18 | `not_applicable` | US-listed securities only (FINRA short interest, SEC 13F, SEC fails-to-deliver); an ADR carries it on its US line | by design |
| r19 | `insufficient_data` | Quarterly history from TDnet tanshin starts 2026-09 (the TDnet list keeps 31 days) and EDINET covers few issuers: surprises use the same period a year earlier (the comparative in the tanshin) until the estimated models have 10 quarters | W1 2026-10-31: TDnet tanshin releases + actuals mapped into earnings_events / expectations (seasonal_rw) |
| r20 | `needs_key` | Needs OPENDART_APIKEY (Korea DART quarterly reports) | user: add OPENDART_APIKEY |
| r21 | `no_source` | Quarterly EPS is not filed machine-readably in the EU / UK (ESEF covers annual reports; most issuers report semi-annually) | - |
| r22 | `no_source` | No free structured quarterly results feed for this market | - |
| r23 | `insufficient_data` | Model expectations need the company's quarterly / half-yearly revenue or EPS including the same period a year earlier (no statements loaded, or annual-only reporting) | statements coverage (W2: regulator-complete statements) |
| r24 | `insufficient_data` | Price reactions need ~250 sessions of prices before the release (market-model estimation window) | by design |
| r25 | `insufficient_data` | No earnings release recorded for this company yet (no regulatory filing, exchange disclosure or provider date matched this listing) | earnings_dates_fetch (Yahoo / exchange schedules, full universe) |
| r27 | `mapping_gap` | ISIN not resolved for this listing (no CUSIP / registry match) | ref-identity: OpenFIGI / exchange lists |
| r28 | `not_applicable` | SEC / US identifiers apply to SEC registrants only | by design |
| r29 | `no_source` | LEI is not carried by this market's filings; GLEIF lookup not wired | ref-identity: GLEIF |
| r30 | `out_of_scope` | OpenFIGI mapping was run for the US and EU expansion only | ref-identity: openfigi refresh (OPENFIGI_APIKEY speeds it up) |
| r34 | `out_of_scope` | Employee counts come from the company profile of the analysis universe | ref-identity |
| r35 | `out_of_scope` | Needs shares outstanding, which is loaded only for issuers with financial statements | 2026-10-27: ratios_kpis + valuation_daily default universe=full (every issuer with loaded statements); statements for the remaining US small caps need sec_xbrl_bulk all_ciks (ref-identity / ingest-us) |
| r36 | `failed` | conformal_envelopes timed out (45 min) on 10-24/25 before the 10-26 fix (90 min, per-region checkpoint); the next run refreshes US / LATAM | sync engine retry (agent A) |
| r37 | `stale` | The 2026-10-23 session bars are missing for EU / UK / APAC; the vol regime needs bars ending on the snapshot date | sync engine: prices per region since watermark (agent A) |
| r38 | `not_applicable` | Factor percentiles are computed among liquid names with enough history only | by design |
| r39 | `stale` | Not computed for the latest session (stale bars or a pending run) | sync engine |
| r40 | `stale` | No bar since <= 2026-10-13 (delisted / acquired names still in the universe, or a halted listing) | ref-identity: retire delisted securities from universe full |
| r41 | `suppressed` | ML forecasts outside the MVP model universe are served (flagged extrapolated) only in the region x liquidity segments where the out-of-sample check of the MVP-trained model passed; elsewhere withheld (per-security reason in the tooltip: no validated skill, too few features or no recent price) | full-scope 2026-10-30: evidence data/state/full_scope.json (ml); re-check after model retrains |
| r42 | `needs_key` | Needs OPENDART_APIKEY (Korea DART financial statements) | user: add OPENDART_APIKEY |
| r43 | `source_unavailable` | CN statements: BaoStock is IP-banned and AKShare covers the curated names only | ingest-global: resume BaoStock with one worker |
| r44 | `no_source` | HKEX filings are PDF only (no XBRL); no free structured source | - |
| r45 | `no_source` | No free structured statements for IN / AU / SG / TH / ID (TW MOPS covers the TW universe only) | - |
| r46 | `not_applicable` | Line item not reported separately by most issuers (industry-specific or optional disclosure) | by design |
| r47 | `insufficient_data` | Scores cover every security with enough inputs (outside the MVP universe anchored to the MVP reference cross-section); a composite needs >= 5 factor scores incl. 2 of quality / profitability / value / growth, so companies without loaded statements, market cap or 126 sessions of prices have none (the tooltip lists the missing inputs per security) | statements for the remaining US small caps: sec_xbrl_bulk all_ciks (ingest-us); a shares-outstanding feed for JP / KR / CN / HK |
| r48 | `out_of_scope` | Ratios and valuation run for every issuer with loaded statements (~4.9k issuers); the rest of the universe has no statements loaded yet | 2026-10-27: ratios_kpis + valuation_daily default universe=full (every issuer with loaded statements); statements for the remaining US small caps need sec_xbrl_bulk all_ciks (ref-identity / ingest-us) |
| r49 | `stale` | Macro exposures not refreshed for this security yet | macro_exposures weekly |
| default | `not_applicable` | Not reported for every company | - |

## Page elements outside BQL

| Element | Reason | Note | Source |
|---|---|---|---|
| `earnings.upcoming.actuals` | `not_yet_published` | Not published yet: the company reports on the listed date | earnings calendar item date |
| `macro.tile.latest` | `not_yet_published` | Next official release on the calendar date | macro release calendar (next_release) |
| `cross_asset.positioning` | `no_source` | No CFTC futures market for this asset | cftc_markets mapping |
| `security.logo_url` | `no_source` | Logos are not stored (no licensed logo source) | - |
| `security.name_local` | `not_applicable` | Local-script name only for non-Latin markets | - |
| `smart_money.overview.cross_fields` | `pipeline_bug` | Fixed 2026-10-27: overview lists now carry the 13F, short-interest and score fields of every row | app.api.routers.smart_money |
| `company.factors.delta_1m` | `stale` | Fixed 2026-10-27: filled by the next scoring run | factor_scores.delta_1m |
