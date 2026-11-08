# Financial-statements gap audit (universe `full`), 2026-11-06

Read-only audit. No code or data was changed. The data comes from serving snapshot `serving_SI20261106T0659446YC6.duckdb`
(tables `universe`, `sec`, `issuers`, `fund`, `coc_latest`; opened read_only), the lake `normalized/fundamentals_canonical`,
`fundamentals_raw` and `filings_index`, and `ops/regulatory_coverage` (run R20261106T065902RQ7M), `ops/job_runs`, `data/state/*.json` and `data/raw/*`.
The full per-security list is in [`statements_missing_2026-11.csv`](statements_missing_2026-11.csv). It has 20,664 rows: every
security that is not `fresh`, with its identifiers, status, latest periods, sources, market cap and basis, reason code and cause.

**Definitions.** The as-of date is 2026-11-06.
- `annual stale`: the latest FY `period_end` is before 2025-05-06 (older than 18 months).
- `interim stale`: the latest Q/H `period_end` is before 2026-02-06 (older than 9 months).

Status values, worst first:
- `none`
- `int_only` (no annual, but an interim exists)
- `stale_ann`
- `ann_only` (no interim at all)
- `stale_int`
- `fresh`

The universe `full` has 26,160 securities, with one issuer per security. ETFs, indices, FX and futures are not in `full`. There are
144 closed-end funds, investment trusts and VCTs (name rule), which are counted as `not_applicable` and excluded from the
denominators. That leaves **26,016 applicable issuers**.

**Market cap basis.** `valuation_latest` only holds a market cap for issuers that already have statements, because shares
outstanding come from the statements. Weighting needs a market cap for the others too, so:
- Valued names (6,316) use the actual value. 87 of them are outliers (more than 31 times the ADV-implied value; see §6) and use the ADV-implied value instead.
- Unvalued names in US / EU / UK use log(mcap) = a(MIC) + 0.806·log(ADV20). This is fitted on 6,040 valued names, R² = 0.86, residual SD 0.43 log10. Treat these as roughly ±2.7x.
- In CN, JP, KR, HK, TW and BR the valued names are curated mega caps. There, an approximate exchange total is spread over the unvalued names by ADV^0.806. The totals, in USD trillions, are labelled estimates: CN 13.0, JP 7.0, KR 2.6, HK 5.5, TW 3.0, BR 0.8.
- 3,030 names have no price, mostly EU/UK names newly added from the register. Their market cap is unknown and they are excluded from the weights.

**Market-cap percentages in Asia are indicative.** Issuer counts are exact.

## 1. Headline

| Metric | Issuers | % of issuers | % of mcap (est.) |
|---|---|---|---|
| Any statement loaded | 9,543 | 36.7 | 87.5 |
| **No statements at all** | **16,473** | **63.3** | **12.5** (USD 17.4 tn of 139.8 tn) |
| Annual ≤ 18 months | 8,247 | 31.7 | 85.5 |
| Fresh (annual ≤ 18 m and interim ≤ 9 m) | 5,486 | 21.1 | 75.6 |
| Stale annual / interim-only / annual-only / stale interim | 513 / 783 / 2,470 / 291 | | |

By issuer count, 91 % of the issuers without statements are in five markets: CN, JP, KR, HK and TW. Their combined market cap is about USD 17 tn.
By market cap, the gap is in the mid and small caps (see §2). The 16 % of large caps (USD 10-200 bn) with no statements are almost all CN, HK, JP and TW names.

## 2. Breakdown

**By region**

| Region | Issuers | None | Int-only | Stale ann | Ann-only | Stale int | Fresh | Any % | Annual-OK % mcap | Fresh % mcap |
|---|---|---|---|---|---|---|---|---|---|---|
| US | 5,099 | 26 | 93 | 40 | 384 | 238 | 4,318 | 99.5 | 97.8 | 92.7 |
| EU | 3,320 | 652 | 3 | 419 | 1,232 | 43 | 971 | 80.4 | 99.0 | 61.3 |
| UK | 1,254 | 475 | 0 | 52 | 682 | 7 | 38 | 62.1 | 99.5 | 52.0 |
| JP | 3,700 | 2,795 | 687 | 2 | 162 | 0 | 54 | 24.5 | 59.4 | 58.7 |
| CN | 5,224 | 5,203 | 0 | 0 | 0 | 0 | 21 | 0.4 | 20.7 | 20.7 |
| KR | 2,651 | 2,636 | 0 | 0 | 0 | 0 | 15 | 0.6 | 74.8 | 74.8 |
| HK | 2,451 | 2,429 | 0 | 0 | 8 | 1 | 13 | 0.9 | 43.9 | 34.5 |
| APAC (TW 1,982) | 1,990 | 1,980 | 0 | 0 | 2 | 2 | 6 | 0.5 | 72.7 | 58.1 |
| LATAM (BR 290) | 323 | 277 | 0 | 0 | 0 | 0 | 46 | 14.2 | 80.4 | 80.4 |
| CA | 4 | 0 | 0 | 0 | 0 | 0 | 4 | 100 | 100 | 100 |

**By exchange (MIC, ≥ 100 issuers)**

| MIC | Issuers | None | Stale ann | Ann-only | Any % | Annual-OK % mcap |
|---|---|---|---|---|---|---|
| XTKS | 3,700 | 2,795 | 2 | 162 | 24.5 | 59.4 |
| XNAS / XNYS / XASE | 3,113 / 1,749 / 231 | 13 / 13 / 0 | 19 / 13 / 8 | 208 / 142 / 34 | 99.6 / 99.3 / 100 | 96.2 / 99.6 / 99.2 |
| XSHE / XSHG | 2,904 / 2,320 | 2,897 / 2,306 | 0 | 0 | 0.2 / 0.6 | 9.3 / 30.9 |
| XHKG | 2,451 | 2,429 | 0 | 8 | 0.9 | 43.9 |
| XKOS / XKRX | 1,820 / 831 | 1,820 / 816 | 0 | 0 | 0.0 / 1.8 | 0.0 / 84.3 |
| XTAI / ROCO | 1,089 / 893 | 1,087 / 893 | 0 | 0 | 0.2 / 0.0 | 69.1 / 0.0 |
| XLON | 1,248 | 474 | 52 | 679 | 62.0 | 99.5 |
| XWAR | 375 | 67 | **151** | 3 | 82.1 | 100 |
| XETR | 308 | 87 | 15 | 33 | 71.8 | 99.7 |
| XPAR | 304 | 41 | 12 | 226 | 86.5 | 99.8 |
| BVMF | 290 | 277 | 0 | 0 | 4.5 | 60.4 |
| XBUL | 245 | 245 | 0 | 0 | 0.0 | n/a (no valid price) |
| XSTO / XMIL / XSWX | 350 / 199 / 147 | 5 / 5 / 0 | 9 / 28 / 0 | 158 / 86 / 124 | 98.6 / 97.5 / 100 | 100 / 97 / 100 |

EU countries with the weakest coverage:
- BG: 245 of 246 have no statements.
- SK: 18 of 23 none.
- CY: 34 none and 33 stale.
- HR: 61 of 67 have a stale annual. The latest ESEF on filings.xbrl.org is FY2022.
- PL: 145 have a stale annual. ESEF lags on filings.xbrl.org.
- DE: 121 none. filings.xbrl.org carries no DE filings, and the Unternehmensregister is not machine-readable.
- RO: 26 none and 21 stale.

**By market-cap bucket**

| Bucket | Issuers | None | Any % | Annual-OK % | Annual-OK % mcap | No-statement mcap (USD bn) |
|---|---|---|---|---|---|---|
| Mega > 200 bn | 91 | 2 | 97.8 | 95.6 | 96.2 | 439 |
| Large 10-200 bn | 1,522 | 243 | 84.0 | 82.4 | 90.8 | 4,601 |
| Mid 2-10 bn | 3,734 | 2,005 | 46.3 | 42.8 | 47.0 | 8,014 |
| Small 0.3-2 bn | 7,544 | 5,327 | 29.4 | 25.7 | 26.7 | 4,761 |
| Micro < 0.3 bn | 10,095 | 7,600 | 24.7 | 20.6 | 24.1 | 445 |
| Unknown (no price) | 3,030 | 1,296 | 57.2 | 42.7 | n/a | n/a |

**By security type**

| Class | Issuers | None | Any % | Annual-OK % | Note |
|---|---|---|---|---|---|
| Common | 25,432 | 16,427 | 35.4 | 30.4 | |
| ADR | 294 | 4 | 98.6 | 95.2 | 136 are annual-only, because foreign private issuers have no 10-Q |
| REIT | 220 | 1 | 99.5 | 98.2 | |
| Preferred | 44 | 39 | 11.4 | 11.4 | BR PN lines are separate issuers. They should map to the ON issuer (mapping gap) |
| SPAC | 26 | 2 | 92.3 | 65.4 | 7 are interim-only (no 10-K yet) |
| Fund / trust | 144 | 87 | 39.6 | 32.6 | `not_applicable`, excluded |

## 3. Top 30 issuers with no statements, by estimated market cap

All 30 market caps are ADV-implied estimates, so the ranking is indicative.
- HK: these are `no_source/pdf_only`, and the Yahoo fallback has not been run for them.
- CN: these are `source_unavailable/blocked_source`. Only CN rows show `cn_code`, which equals the ticker.
- JP and TW: these are `out_of_scope/scope_gap`.

| # | Ticker | Name | MIC | ISIN / local id | Est. mcap USD bn | Cause |
|---|---|---|---|---|---|---|
| 1 | 285A.T | Kioxia Holdings | XTKS | EDINET filer (annual FY2026-03 filed 2026-06-24) | 216 | scope_gap |
| 2 | 0981.HK | SMIC | XHKG | KYG8020E1199 | 69 | pdf_only |
| 3 | 300308.SZ | Zhongji Innolight | XSHE | 300308 | 68 | blocked_source |
| 4 | 0992.HK | Lenovo Group | XHKG | HK0992009065 | 62 | pdf_only |
| 5 | 2513.HK | Z.AI | XHKG | CNE100007DH9 | 60 | pdf_only |
| 6 | 6869.HK | YOFC | XHKG | CNE100001T72 | 55 | pdf_only |
| 7 | 300502.SZ | Eoptolink | XSHE | 300502 | 54 | blocked_source |
| 8 | 1888.HK | KB Laminates | XHKG | KYG5257K1076 | 52 | pdf_only |
| 9 | 688825.SS | CXMT | XSHG | 688825 | 50 | blocked_source |
| 10 | 5803.T | Fujikura | XTKS | EDINET filer | 47 | scope_gap |
| 11 | 1347.HK | Hua Hong Semi | XHKG | HK0000218211 | 45 | pdf_only |
| 12 | 3750.HK | CATL (H) | XHKG | CNE100006WS8 | 44 | pdf_only + **A+H mapping gap** (300750.SZ has statements) |
| 13 | 2269.HK | WuXi Biologics | XHKG | KYG970081173 | 43 | pdf_only |
| 14 | 0100.HK | MiniMax-W | XHKG | KYG6181S1093 | 42 | pdf_only |
| 15 | 1548.HK | GenScript Bio | XHKG | KYG3825B1059 | 40 | pdf_only |
| 16 | 3308.HK | ZJ Innolight | XHKG | CNE100007SR6 | 38 | pdf_only |
| 17 | 603986.SS | GigaDevice | XSHG | 603986 | 37 | blocked_source |
| 18 | 688256.SS | Cambricon | XSHG | 688256 | 36 | blocked_source |
| 19 | 002384.SZ | Dongshan Precision | XSHE | 002384 | 35 | blocked_source |
| 20 | 2359.HK | WuXi AppTec (H) | XHKG | CNE100003F19 | 35 | pdf_only |
| 21 | 2628.HK | China Life (H) | XHKG | CNE1000002L3 | 34 | pdf_only |
| 22 | 2899.HK | Zijin Mining (H) | XHKG | CNE100000502 | 33 | pdf_only |
| 23 | 9992.HK | Pop Mart | XHKG | KYG7170M1033 | 32 | pdf_only |
| 24 | 2454.TW | MediaTek | XTAI | TW0002454006 | 31 | scope_gap |
| 25 | 300394.SZ | TFC Optical | XSHE | 300394 | 30 | blocked_source |
| 26 | 603259.SS | WuXi AppTec (A) | XSHG | 603259 | 28 | blocked_source |
| 27 | 600183.SS | Shengyi Tech | XSHG | 600183 | 28 | blocked_source |
| 28 | 600487.SS | Hengtong Optic | XSHG | 600487 | 28 | blocked_source |
| 29 | 1024.HK | Kuaishou-W | XHKG | KYG532631028 | 27 | pdf_only |
| 30 | 5801.T | Furukawa Electric | XTKS | EDINET filer | 26 | scope_gap |

The largest names with a stale or missing annual are SpaceX (SPCX; IPO, only 10-Qs so far, `not_yet_published`), Genmab (FY2024
only; see §4), Taiyo Yuden, Lasertec and Ibiden (JP; TDnet interim only), Honeywell Aerospace (spin-off, not yet published),
Novozymes (FY2023) and Tenaris (FY2024).

## 4. Root causes

Reason codes follow `contracts/field_availability.yaml`. For US, EU and UK the per-issuer reason comes from `ops/regulatory_coverage`.

| Cause (code) | None | Other gaps | Est. mcap USD bn | Where | Evidence |
|---|---|---|---|---|---|
| **Scope gap: the source is wired, but the issuer is not targeted** (`out_of_scope`) | 7,688 | 851 | 4,959 | JP 3,484, KR 2,636, TW 1,980, BR 277 | The EDINET, DART bulk, CVM, MOPS, AKShare and BaoStock adapters default to `seed_universe()` = `mvp_global.csv` (271 names). The data for the other issuers is already on disk (see the spot checks) |
| **Blocked source** (`source_unavailable`, r43) | 5,203 | 0 | 10,311 | CN | BaoStock is IP-banned. AKShare is run for 21 seed issuers only. `cn_baostock` failed or was partial in 5 of 12 runs over 30 days |
| **PDF-only filings** (`no_source`, r44 / r21) | 2,793 | 1,953 | 11,831 | HK 2,429, UK AIM 364; EU/UK half-years 1,922 | HKEXnews is PDF/HTML only. ESEF covers annual reports only. AIM companies are not on ESEF/NSM |
| **No source; Yahoo fallback pending** (`no_source`) | 757 | 4 | 13 | EU 653, UK 104 | No ESEF or OAM filing for the LEI. 94 % have no price either |
| **Foreign filer** (`not_applicable` interim) | 0 | 612 | 4,362 | US 20-F / 40-F | No 10-Q duty. 6-K interims are rarely XBRL |
| **Refresh lag / repository lag** (`stale`) | 0 | 537 | 616 | EU 418 (PL, HR, CY, BE, LU), UK 50, US 39 | A newer filing exists but has not been loaded, or filings.xbrl.org lags the national OAM |
| **Parse / normalise failure** (`pipeline_bug`) | 17 | 27 | 40 | US 36, UK 5, EU 3 | Raw facts are loaded, but no core item was normalised |
| **Not yet published** | 3 | 71 | 1,730 | US IPOs and spin-offs | Only 10-Qs since listing |
| **Mapping gap** (`mapping_gap`) | small | | | | Identifier links are mostly fine: EDINET code is set for 2,785 of 2,797 JP gaps, `cvm_code` for 261 of 277 BR gaps, and LEI for every EU/UK gap. The real mapping gaps are listed below |
| **Yahoo fallback not yet run** (cross-cutting) | 16,804 | | 18,308 | all non-US (none or interim-only, with a Yahoo symbol) | The `yf` step of `register_backfill` covered only EU/UK register issuers (1,280 tickers). Only 13 issuers were attempted and are still empty |

The mapping gaps found:
- 511 HK H-share lines (ISIN `CNE1…`) are separate issuers from their A-share twin. One example: CATL 3750.HK vs 300750.SZ.
- 39 BR preferred lines (PN, "…4.SA") are separate issuers.
- 12 SEC companyfacts CIKs and 1 ESEF LEI are in raw but carry no `issuer_id`.
- `fundamentals_raw` holds 3,660 unmapped source entities: 1,108 `sec_fsds`, 1,040 `yfinance_fund`, 888 `sec_edgar`, 352 `tdnet`, 261 `esef`, 11 `fca_nsm`. Almost all of them are outside the universe.

**Spot checks.** These used single polite requests, with robots.txt checked first, or files already on disk.

| # | Issuer (status) | Source checked | Result | Root cause confirmed |
|---|---|---|---|---|
| 1 | Kioxia 285A.T (none) | EDINET daily lists on disk | Annual reports (docType 120) for FY2025-03 and FY2026-03, plus a half-year report filed 2026-11-06. `xbrlFlag=1` and `csvFlag=1` | Scope gap. Only the 40 seed issuers are downloaded |
| 2 | Fujikura 5803.T, Taiyo Yuden 6976.T | EDINET lists on disk | The same pattern: annual and semi-annual CSV available | Scope gap |
| 3 | Samsung Electro-Mechanics 009150.KS (none) | DART bulk zip `2025_4Q_PL` on disk | 63 rows for `[009150]` | Scope gap (`all_listed=False`), no download needed |
| 4 | Equatorial EQTL3.SA (none) | CVM `dfp_cia_aberta_2025.zip` on disk | 2 DFP rows for CD_CVM 020010. The DRE file covers 440 companies | Scope gap |
| 5 | Zhongji Innolight 300308.SZ (none) | CNINFO hisAnnouncement (1 POST) | 2025 annual and 2026 half-year reports, **PDF** | Blocked or scoped source. CNINFO itself has PDFs only. Structured data needs AKShare-Sina or Yahoo |
| 6 | SMIC 0981.HK (none) | HKEXnews titleSearch (1 request) | 198 documents. The 2025 Annual Report and 2026 Interim Report are **PDF** | PDF only |
| 7 | First Property FPO.L (AIM, none) | FCA NSM, per-LEI query (1 POST) | 315 items, e.g. "Notice of Results" 2026-10-19 as **HTML RNS**. No ESEF JSON | PDF/HTML only. Results tables could be parsed from HTML |
| 8 | Genmab GMAB.CO (stale annual, FY2024) | filings.xbrl.org entity filings | 20 filings, including **FY2025 (added 2026-04-07)**, Q1 2026 and H1 2026 ESEF | Ingest gap: the filing exists but has not been loaded. Not `no_source` |
| 9 | VIS (HR, stale FY2022) | filings.xbrl.org | Only FY2021 and FY2022 | Repository lag. A national OAM (ZSE) is needed |
| 10 | KAP AG (DE, none) | filings.xbrl.org | 404 (no entity) | No source. Use the Yahoo fallback |
| 11 | SOLV Energy (US, interim only, flagged `pipeline_bug`) | SEC companyfacts | Forms: S-1MEF and 10-Q only | **Misclassified**. It should be `not_yet_published` |
| 12 | FedEx Freight FDXF (US, annual only) | SEC companyfacts | 10-K and 10-Q facts present (198 us-gaap concepts) | Real `pipeline_bug`: the 10-Q is not normalised |
| 13 | Yahoo: 285A.T, 0981.HK, 009150.KS, 300308.SZ | yfinance | All four return 3-4 annual and 2 or more quarterly periods | The fallback works. It is simply not run |

**Compliance finding.** The robots.txt of `mopsov.twse.com.tw` is `User-Agent: * / Disallow: /`. `mops_tw.py` uses
`mopsov/server-java/*`. Do not extend that adapter. Review the current runs.

## 5. Sources

Coverage is canonical issuers in `full`. Freshness counts issuers whose latest FY (Q/H) from that source is no older than 18 (9) months.
"Fail 30 d" means failed and partial runs out of all runs, from `ops/job_runs` since 2026-10-07.

| Source | Covers | We ingest | Issuers | Annual fresh / interim fresh | Last FY / Q end | PIT (knowledge_date) | Fail 30 d | Left out |
|---|---|---|---|---|---|---|---|---|
| SEC companyfacts (`sec_edgar`) | Every XBRL filer | Bulk zip plus nightly, all register CIKs | 5,154 | 5,090 / 4,739 | 2026-10-03 | Acceptance date (0.03 % estimated) | sec_xbrl_facts 3/16; bulk 1/5 | FPI 6-K interims; 36 normalise gaps; 888 CIKs outside the universe |
| SEC FSDS (`sec_fsds`) | 20-F / 40-F IFRS facts, 2009→ | 71 quarterly extracts, register FPIs | 684 | 239 / 332 | 2026-06 | Filing date | 0/5 | Lags about 1 quarter |
| ESEF, filings.xbrl.org | Voluntary ESEF repository (patchy: no DE/IE; PL/ES/SE lag; some quarterly ESEF) | Census plus per-LEI sweep for EU/UK register LEIs | 2,375 | 1,658 / 185 | 2026-09 | `date_added` (conservative; 4 % estimated) | esef_filings 1/13; lei_sweep 0/46 | Newer filings not reloaded (Genmab), repository lag (HR/CY/PL), voluntary interim ESEF mostly unused |
| National OAMs (AMF/DILA, AFM, FSMA, CNMV, eMarket, Oslo, Nasdaq Nordic) | ESEF and PDF annual reports per country | Seed and universe filers only | 21-178 each | e.g. Nordic 137/46, DILA 58/5 | 2025-12 to 2026-06 | Publication date (8-13 % estimated) | 0-3 partial per 11 runs | DE (Unternehmensregister), BG, HR, CY, RO, SK, SI OAMs not wired; half-years are PDF |
| FCA NSM | UK ESEF/UKSEF annual reports, RNS | Per-LEI (`fca_nsm` step: 371 LEIs done) | 91 | 51 / 1 | 2026-09 | Publication timestamp | 0/14 | AIM (not ESEF); half-years (RNS HTML/PDF); market-wide paging blocked by WAF |
| Companies House bulk iXBRL | Small-company iXBRL accounts | Ranged reads for 1,132 target company numbers | 9 | 6 / 0 | 2026-06 | Filing date (50 % estimated) | 0/4 (0 matches in the last run) | PLC and AIM group accounts are PDF; the REST API key is not set (`COMPANIES_HOUSE_APIKEY` empty) |
| EDINET (JP) | Every annual and semi-annual report, XBRL→CSV | **Lists 2016→ landed. CSVs for 40 seed issuers only.** Key is set | 40 | 40 / 31 | 2026-08 | `submitDateTime` (5 % estimated) | 1/12 | 3,648 JP issuers |
| TDnet (JP) | Tanshin summaries (31-day window) | All XBRL in the window since 2026-09 | 1,066 | 177 / 727 | 2026-09 | Exact | 1/13 | History before 2026-09; annual figures until the May tanshin season |
| DART bulk (KR) | All listed K-IFRS BS/PL/CF, 2015Q4→ | **129 zips landed. Normalised for 15 seed issuers** | 15 | 15 / 15 | 2026-06 | **Estimated** (the file has no filing date; 16 %) | 0/3 | 2,636 KR issuers |
| OpenDART (KR) | Filings list plus statements | Not running: **`OPENDART_APIKEY` is empty** (presence only checked) | 0 | | | `rcept_dt` (exact) | 10/10 skipped | PIT dates for DART bulk |
| BaoStock (CN) | A-share ratios, `pubDate` | 21 seed issuers; IP-banned | 21 (no FY rows) | 0 / 0 | | `pubDate` | 5/12 failed or partial | All of CN |
| AKShare (Sina + CNINFO) (CN) | Full CAS statements, all A-shares | 21 seed issuers | 21 | 21 / 21 | 2026-09 | Announcement date (67 % estimated) | 2/12 | 5,203 CN issuers |
| HKEXnews (HK) | Announcements, PDF only | `filings_index` for 25 issuers (no facts) | 0 facts | | | `DATE_TIME` | 1/12 | Every HK statement |
| CVM (BR) | DFP/ITR for every Brazilian company | **Yearly zips landed. 13 seed CVM codes normalised** | 13 | 13 / 13 | 2026-09 | `DT_RECEB` (exact) | 0/5 | 277 BR issuers |
| CMF (CL), BIVA (MX), SMV (PE) | Per-issuer IFRS XBRL | Seed only | 7 / 18 / 3 | All fresh | 2026-09 | Publication date | CMF 2/11 | Nothing material (LATAM non-BR is fully covered) |
| MOPS (TW) | IFRS iXBRL | 2 seed issuers. **robots.txt disallows** | 2 | 2 / 2 | 2026-09 | Upload date | 0/3 | 1,980 TW issuers |
| Yahoo (`yfinance_fund`) | About 4 annual and 5 quarterly periods, global | EU/UK register issuers plus seed (2,196 issuers) | 2,196 | 1,986 / 1,088 | 2026-09 | **Estimated** (period_end + 120/60 d; 98 % estimated) | 3/14 | All of CN/JP/KR/HK/TW/BR outside the seed (16,804 issuers with a Yahoo symbol never attempted) |

## 6. Side findings that bias coverage metrics

1. **Price identity break (59 listings).** XBUL, XCYS, XMAL, XBRA and XZAG listings have no Yahoo or Polygon symbol, yet each carries 550 bars. 31 of them share a bare ticker with a US stock: CVS on XMAL has a close of $91 and ADV of $1.0 bn, which looks like CVS Health. ETR, HON, REG and ROL show the same pattern. The bars are probably joined on the bare local symbol. These names were excluded from the market-cap weights.
2. **Market-cap outliers in `valuation_latest`.** 87 valued names are more than 31 times their ADV-implied value. Examples: AKTX at $1.2 tn, TLK ADR at $1.1 tn, 8766.T Tokio Marine at $1.6 tn (close $3.82), RO.SW at $392 bn and RPAY at $380 bn. These look like ADR-ratio, share-unit or price-divisor errors. They inflate the US and JP totals.
3. `fundamentals_normalize` dropped currency-mismatch facts in 11 of 39 runs. The latest run dropped 9,754 facts across 4 issuers.

## 7. Recommendations

Gains are counted against the 26,016 applicable issuers and USD 139.8 tn estimated market cap. "Mcap %" is the share of total
market cap that moves into the covered column.

| # | Region | Action | Mapping key | PIT | Effort | Gain: issuers | Gain: mcap % |
|---|---|---|---|---|---|---|---|
| 1 | CN | Run `cn_akshare` (Sina statements, already allow-listed) for **all** A-shares instead of the seed. Use the CNINFO periodic-report list (`stock_zh_a_disclosure_report_cninfo`) for exact announcement dates. Keep BaoStock off; do not work around the IP ban. About 5,200 × 3 calls at 1/s, roughly 4.5 h, split into resumable chunks. Yahoo is the fallback | `cn_code` = ticker | CNINFO 公告时间 (exact) | M | 5,203 | 7.4 |
| 2 | JP | EDINET `all_listed=True`, with `max_docs` raised for a one-off backfill. The day lists are already on disk, so only `type=5` CSV downloads remain: about 3,700 issuers × 2 docs per year, roughly 2 h per history-year at 1/s. Q1/Q3 come from TDnet going forward; Yahoo quarterly fills the history | `edinet_code` (2,785 of 2,797 set) / secCode | `submitDateTime` (exact) | S-M | 3,648 (2,797 none + 687 int-only + 162 ann-only) | 2.1 |
| 3 | HK | (a) Yahoo statements fallback for every HK listing (S). (b) Link 511 H-shares to their A-share issuer, using the HKEX A+H list or AKShare `stock_zh_ah_name`, after #1 (S). (c) PDF parsing as a later phase (L, see below) | ISIN / A+H pair | Yahoo is estimated. Use HKEXnews `DATE_TIME` of the results announcement as `knowledge_date` | S (+L) | 2,429 | 2.2 |
| 4 | KR | `dart_bulk all_listed=True renormalize=True`. Uses the 129 zips on disk, no network. Then set `OPENDART_APIKEY` (user action) so `list.json` `rcept_dt` replaces the estimated dates | 6-char stock code | `rcept_dt` once the key is set | S | 2,636 | 0.5 |
| 5 | TW | Do **not** scale `mops_tw` (robots disallows it). Use the TWSE/TPEx OpenAPI (`openapi.twse.com.tw` t187ap06/07 L/O, latest quarter, official open data) plus Yahoo for history | Ticker / `co_id` | OpenAPI publication date, else estimated | M | 1,980 | 0.8 |
| 6 | BR | `cvm` with every `cvm_code` (zips on disk). Map the PN lines to the ON issuer (39 preferreds) | `cvm_code` (261 of 277) / CNPJ | `DT_RECEB` (exact) | S | 277 | 0.2 |
| 7 | EU/UK interim | (a) Yahoo quarterly fallback for the 1,964 annual-only and stale-interim issuers (S). (b) Ingest the voluntary interim ESEF already on filings.xbrl.org (Genmab-type) (S). (c) Parse UK half-year RNS **HTML** tables from NSM per LEI (M). (d) Parse OAM half-year PDFs (L) | LEI | Publication timestamp; Yahoo is estimated | S → L | 1,964 (interim) | 5.9 |
| 8 | US FPI | Yahoo quarterly for 612 20-F/40-F issuers, or parse 6-K exhibit 99.1 HTML tables (M) | CIK | 6-K acceptance time | S / M | 612 (interim) | 3.1 |
| 9 | EU/UK refresh | Re-sweep ESEF per LEI for the 471 stale annuals. For HR, CY, PL and RO, add the national OAM (ZSE, CSE, GPW ESPI/EBI) where it is machine-readable and robots allow it | LEI | OAM date | S-M | 471 (stale) | 0.1 |
| 10 | EU/UK no source | Finish the `register_backfill yf` step for 1,125 issuers (EU 653 no-ESEF, UK AIM 364 and others). Fix the 59 price-identity breaks first | yf_ticker | Estimated | S | 1,125 | <0.1 |
| 11 | US | Fix 36 `pipeline_bug` cases, e.g. FDXF 10-Q not normalised. Reclassify only-10-Q-since-S-1 cases such as SOLV as `not_yet_published` | CIK | Acceptance date | S | 36 | <0.1 |

Effect of #1 to #6: issuers without any statement fall from 16,473 to about 1,150, and any-statement coverage rises from 36.7 % to about
96 % of issuers and from 87.5 % to about 99.9 % of estimated market cap. Annual-OK coverage rises from 31.7 % to about 93 % of issuers.
"Fresh" coverage by market cap rises from 75.6 % to about 88 % after #1 to #6, and to about 97 % once #7 and #8 are done.

**PDF parsing for UK/EU half-years and HK (#3c, #7d): feasible, but L effort and medium quality.**
- **How:** pdfplumber or camelot table extraction. Locate the statement pages by keywords, for example "Condensed consolidated income statement", "綜合損益表" or "Consolidated Statement of Profit or Loss".
- **Quality:** this works on text-layer PDFs, which covers most HK and LSE main-market filers. Expect about 80-90 % correct extraction for 10-15 core lines (revenue, operating profit, net income, EPS, total assets, equity, CFO) once these are handled: units ("HK$'000", "RMB million"), parentheses for negatives, prior-period columns and two-language layouts.
- **Where it fails:** scanned and image-only PDFs need OCR, and multi-column bilingual HK reports cause errors. Reliability needs per-issuer layout memory and cross-checks such as assets = liabilities + equity, and summed segments against the total.
- **Plan:** pilot on the top 200 HK and top 200 UK names by market cap, about 2-3 weeks. Keep Yahoo as the primary source and PDF as the official cross-check.
- **PIT:** use the HKEXnews or NSM announcement timestamp as `knowledge_date`. That is better than Yahoo's estimated dates.
- **UK detail:** results RNS on NSM are mostly **HTML**, which is easier to parse than PDF (M).

**PIT summary.** Exact dates are available from SEC, EDINET, TDnet, CVM, CMF, BIVA, NSM, HKEXnews and CNINFO.
- Estimated today: Yahoo (98 % of its rows) and DART bulk.
- AKShare dates are 67 % estimated because Sina rows lack 公告日期. Join CNINFO dates per period to fix this.
- Every Yahoo-sourced row keeps `knowledge_date_estimated=true`. Once an official announcement timestamp exists (HKEX, NSM, CNINFO), use it to stamp the row.
