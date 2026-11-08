# Coverage v3: stabilise, statements for all, new datapoints (plan, 2026-11)

Sources: docs/audits/APP_MAP_AND_GAPS_2026-11.md, docs/audits/STATEMENTS_GAP_AUDIT_2026-11.md (+ statements_missing_2026-11.csv).

## Phase A: Stabilise (first; the backfills need headroom)
| # | Fix | Effort |
|---|---|---|
| A1 | Stop the `ratios_kpis` / `tech_levels` out-of-memory failures, which have recurred since 11-03 and left technicals 7 days stale. Split the work by region, use chunked or streaming writes, and fix the retry at reduced memory. | S |
| A2 | Run heavy jobs strictly one at a time: any job at 7 GB or more holds the heavy slot exclusively. Spill to disk, and add a floor of 3 GB free before admitting a job. Context: 87 of 950 runs had less than 2 GB free. | M |
| A3 | Cap each sync pass, set per-job timeouts from p95 run history, and stop overlapping passes. Context: passes ran 33–39 h. | M |
| A4 | Give PAUSE files an expiry and status in the UI, unpause the filler, and drain the 15 stuck campaigns. | S |
| A5 | Get `dq_checks` running again (last run 10-31). Add date sanity checks: insider dates in 2047, and future dates generally. | S |
| A6 | Price and identity fixes: 59 small EU listings carry a US company's prices (ticker collision); 87 implausible market caps (e.g. 8766.T at $1.6 tn). | S/M |
| A7 | Cache `/system/status` (p95 5.4 s, 442 server errors). Fix the Debt tab returning 404 for ticker URLs. | S |
| A8 | Label composite scores as descriptive (out-of-sample AUC about 0.49). Show "signal" wording only behind an out-of-sample gate. | S |
| A9 | Robots.txt compliance: TDnet, the BoE IADB endpoint, and Taiwan MOPS (mopsov). Switch each to an allowed source or disable it (user decision). | S |

## Phase B: Statements for all
Today 16,473 of 26,016 issuers (63%) have no statements, about 12.5% of market cap. 91% of the gap is CN, JP, KR, HK and TW. The main root cause is scope: these sources only fetch the 271-name `mvp_global` seed, even though the bulk files are already on disk.

| # | Region | Action | Issuers gained / % of market cap | Effort |
|---|---|---|---|---|
| B1 | JP | EDINET `all_listed` CSV backfill, with exact filing dates for point-in-time | 3,648 / 2.1% | S-M |
| B2 | KR | Renormalise DART bulk for `all_listed` from the zips on disk; add the OpenDART key when the user sets it | 2,636 / 0.5% | S |
| B3 | BR | CVM for every `cvm_code`; map PN preferreds to their ON issuer | 277 / 0.2% | S |
| B4 | HK | Yahoo statements fallback; link 511 H-shares to their A-share issuer | 2,429 / 2.2% | S |
| B5 | CN | AKShare-Sina for all A-shares, with CNINFO dates for point-in-time; BaoStock stays off | 5,203 / 7.4% | M |
| B6 | TW | TWSE/TPEx OpenAPI plus Yahoo, replacing MOPS | 1,980 / 0.8% | M |
| B7 | EU/UK interims | Yahoo quarterly, then voluntary interim ESEF, then UK RNS HTML tables (PDF parsing later, L) | 1,964 / 5.9% | S→M |
| B8 | US foreign filers | Interims from Yahoo quarterly or 6-K exhibit tables | 612 / 3.1% | S/M |
| B9 | Loader bugs | Load missing ESEF filings (e.g. Genmab FY2025); fix the SOLV misclassification (`pipeline_bug` → `not_yet_published`); spot-check others | n/a | S |

- **Target:** issuers with no statements fall from 16,473 to about 1,150; any-statement market-cap coverage rises to about 99.9%.
- **Rules:** every value carries a source tag and a knowledge date. Yahoo-sourced values are labelled as a fallback, and regulator-sourced values take precedence.

## Phase C: New datapoints, mostly from data we already have
| # | Datapoint | Source | Effort |
|---|---|---|---|
| C1 | Segments | SEC FSDS segment rows (downloaded but currently dropped), ESEF/EDINET dimensions | S/M |
| C2 | Guidance | US 8-K exhibit 99 text. JP forecasts only from an allowed source; the TDnet-derived ones depend on A9. | S/M |
| C3 | Dividend forecast | Results-summary forecast tags plus a model, labelled as model-based | S |
| C4 | FX forwards | Covered interest parity from the money-market rates already ingested | S |
| C5 | Short volume and positions | FINRA Reg SHO daily files, JPX short positions, HK SFC | S |
| C6 | Corporate actions | M&A, delistings and tender offers from the EDGAR form index | M |
| C7 | Fund holdings and flows | SEC N-PORT bulk files (also gives an index-membership proxy) | M |
| C8 | Litigation | XBRL loss-contingency tags, plus CourtListener API | S/M |
| C9 | Credit ratings | ESMA RADAR | M |
| C10 | Options / implied volatility | Polygon free EOD (check the free-tier limits) | M |

Out of scope with free sources: consensus estimates and transcripts, which need a licence (e.g. LSEG I/B/E/S).

## Execution
- Agent A runs Phase A.
- Agent B writes the Phase B code in parallel, but runs no heavy backfills until A1–A3 are live.
- Agent C (Phase C) starts after A.
- Token-lean mode, at most 2 agents at once.

## Progress
- [ ] A Stabilise
- [ ] B Statements for all
- [ ] C New datapoints
