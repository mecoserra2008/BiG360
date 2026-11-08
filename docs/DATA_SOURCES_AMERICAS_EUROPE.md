# BiG Equity 360: Data Sources for the Americas, Europe, UK and Global Reference Data

*Research date: 2026-10-10. Unless a row is marked "docs only" or "unverified", every endpoint below was called live from this machine (Python 3.13.7, `.venv`) on that date.*
*Scope: US (SEC/FINRA), Massive (formerly Polygon), yfinance/Stooq, EU (ESEF, ESMA FIRDS, GLEIF, OpenFIGI, national PDMR registers), UK (Companies House, FCA NSM), LATAM (CVM/B3 and others), Canada.*
*Japan, Korea and China are covered in a separate document.*

---

## 0. TL;DR

| Need | US | EU / UK | LATAM | Canada |
|---|---|---|---|---|
| **Security master / IDs** | SEC `company_tickers_exchange.json` (CIK↔ticker↔exchange) + Massive `/v3/reference/tickers` (FIGI, SIC) | ESMA FIRDS `FULINS_E` (ISIN↔LEI↔MIC↔CFI) + GLEIF ISIN-LEI file + OpenFIGI (ISIN→ticker/exch) | CVM `cad_cia_aberta.csv` + FCA `valor_mobiliario` (CNPJ↔B3 ticker); EDGAR for ADRs | EDGAR (40-F/10-K filers) + OpenFIGI |
| **Daily prices** | Massive grouped-daily (1 call = whole market) → yfinance fallback/backfill | yfinance (suffixes `.L .PA .AS .DE ...`) | yfinance (`.SA .MX .BA .SN .CL .LM`); B3 COTAHIST (official, free) | yfinance `.TO .V .CN .NE` |
| **Fundamentals (PIT)** | SEC companyfacts / FSDS | filings.xbrl.org xBRL-JSON (ESEF, patchy coverage) + FCA NSM ESEF zips (UK) + yfinance (no PIT) | CVM DFP/ITR (excellent, PIT `DT_RECEB`) + EDGAR 20-F IFRS | EDGAR companyfacts (`ifrs-full`) for 40-F filers, else yfinance |
| **Insider** | SEC Form 3/4/5 XML (daily) + quarterly insider datasets | BaFin CSV export (best), FCA NSM "DSH" RNS, AFM CSV/XML, AMF BDIF JSON+PDF; ES/PT HTML only | CVM VLMO (monthly) + **EDGAR Form 4 for LATAM FPIs (new since 2026, HFIAA)** | SEDI (no free bulk) |
| **Filings text** | EDGAR Archives + EFTS full-text search | FCA NSM (UK, full RNS text); AMF BDIF; ESEF reports | CVM IPE (all fatos relevantes, PDFs) | SEDAR+ (no API; paid CSA DDS) |

**Keys to register (all free):** Massive/Polygon (already in `.env` as `Polygon_APIKEY`, but the `.env` file is currently empty), OpenFIGI (strongly recommended), Companies House, and optionally CMF Chile. Signup URLs are in §11.

**Dead or degraded:** Stooq now requires an API key and serves a JavaScript bot wall (unusable headless). SEDAR+ has no public API. Massive fundamentals (`/stocks/financials/v1/*`) are paywalled at Stocks Advanced ($199/mo). ESAP (the EU single access point) does not go live until July 2027. Mexico, Argentina, Colombia, Peru and Chile have no free machine-readable bulk feed for issuer financials.

---

## 1. United States: SEC EDGAR

### 1.1 Fair-access rules (mandatory)
- **≤ 10 requests/second per user, across all machines.** Use **8 req/s** with a token bucket. Excess traffic gets the IP blocked (typically for 10 minutes, and repeat offenders for longer).
- **Declared User-Agent is required:** `User-Agent: BiG Equity 360 research mecoserra2008@gmail.com` (env var `SEC_USER_AGENT`, already in `.env.example`). Send `Accept-Encoding: gzip, deflate`.
- No auth or key. No CORS (backend only). Docs: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>, <https://www.sec.gov/about/developer-resources>.
- Licensing: public domain US government data.

### 1.2 Reference: tickers ↔ CIK
| URL | Content (verified) |
|---|---|
| `https://www.sec.gov/files/company_tickers.json` | `{"0":{"cik_str":1045810,"ticker":"NVDA","title":"NVIDIA CORP"},...}` |
| `https://www.sec.gov/files/company_tickers_exchange.json` | `{"fields":["cik","name","ticker","exchange"],"data":[[320193,"Apple Inc.","AAPL","Nasdaq"],...]}` |

- Live count: **10,435 rows / 8,017 unique CIKs**. By exchange: Nasdaq 4,376, NYSE 3,290, OTC 2,542, CBOE 44, **TXSE 7** (the new Texas Stock Exchange), and 176 with null exchange. Both files are refreshed daily; Last-Modified was 2026-10-07.
- Pitfalls: one CIK can have several tickers (share classes, preferreds, warrants). Class shares use `-` here (`BRK-B`), which matches Yahoo, while Massive uses `.` (`BRK.B`). The files contain no ISIN or CUSIP. Use OpenFIGI or Massive for FIGI.

### 1.3 Submissions API (company metadata + filing list)
`GET https://data.sec.gov/submissions/CIK##########.json` (10-digit zero-padded)
- Top-level keys (verified): `cik, entityType, sic, sicDescription, ownerOrg, insiderTransactionForOwnerExists, insiderTransactionForIssuerExists, name, tickers, exchanges, ein, lei, description, website, investorWebsite, category, fiscalYearEnd, stateOfIncorporation, addresses, phone, flags, formerNames, filings`.
- `filings.recent` is columnar. Keys: `accessionNumber, filingDate, reportDate, **acceptanceDateTime**, act, form, fileNumber, filmNumber, items, core_type, size, isXBRL, isInlineXBRL, isXBRLNumeric, primaryDocument, primaryDocDescription`.
  - Sample: `{'accessionNumber':'0001140361-26-039158','filingDate':'2026-10-08','reportDate':'2026-10-06','acceptanceDateTime':'2026-10-09T02:30:32.000Z','form':'4','primaryDocument':'xslF345X06/form4.xml'}`
- `recent` holds at least 1 year or 1,000 filings. Older filings are in `filings.files[]` (e.g. `CIK0000320193-submissions-001.json`).
- **PIT field:** `acceptanceDateTime` (UTC, to the second). Latency is under 1 second after dissemination.
- `items` gives the 8-K item numbers (e.g. `2.02,9.01`), which is useful for event tagging without downloading the document.

### 1.4 XBRL APIs
| Endpoint | Example | Notes |
|---|---|---|
| companyfacts | `https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json` | All facts for one company. Apple has `dei` and 503 `us-gaap` tags. Each fact is `{start,end,val,accn,fy,fp,form,filed,frame}` |
| companyconcept | `.../companyconcept/CIK0000320193/us-gaap/AccountsPayableCurrent.json` | One tag |
| frames | `.../frames/us-gaap/AccountsPayableCurrent/USD/CY2024Q4I.json` | Cross-section of all companies for one period. Returned 3,770 companies. Row: `{accn,cik,entityName,loc,end,val}`. `ccp` format is `CY2024` (duration year), `CY2024Q2` (quarter) or `CY2024Q4I` (instant) |

- Sample fact: `{'start':'2026-03-29','end':'2026-06-27','val':109417000000,'accn':'0000320193-26-000020','fy':2026,'fp':'Q3','form':'10-Q','filed':'2026-07-31','frame':'CY2026Q2'}`
- **PIT:** `filed` (date only). Join `accn` to submissions `acceptanceDateTime` to get a timestamp. Restated values show up as **new facts with a later `filed`/`accn`**, so keep every version (that is what makes the data point-in-time).
- **Foreign filers:** 20-F, 40-F and 6-K filers appear under the **`ifrs-full`** taxonomy. Verified: Petrobras (`ifrs-full` 397 tags, `us-gaap` 266), Royal Bank of Canada (`ifrs-full` 298, in CAD), Toyota (JPY). Values are in the reporting currency, so FX normalisation is required.
- Processing delay is under 1 minute after acceptance.
- Pitfalls: `fy`/`fp` describe the *filing's* fiscal period, not the fact's period, so always use `start`/`end`. `frame` is only assigned to the one fact the SEC deems canonical per period. Companies use different tags for the same concept (e.g. `Revenues` vs `RevenueFromContractWithCustomerExcludingAssessedTax`), so we need a tag-mapping registry.

### 1.5 Bulk zips (rebuilt nightly around 03:00 ET)
| File | Size (HEAD, 2026-10-10) | Use |
|---|---|---|
| `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip` | **1.41 GB** | Initial load and weekly full refresh of all companyfacts |
| `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip` | **1.57 GB** | Initial load and weekly refresh of all submissions |

Recommendation: do the full zips **weekly (Sunday)**. On other nights, update incrementally via the daily index (§1.6) and fetch per-CIK JSON only for companies that filed.

### 1.6 Indexes: the efficient way to get every Form 4 and every new filing
- **Daily index** (published around 22:00 ET each business day): `https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261009.idx` (627 KB). Fixed-width columns: `Form Type | Company Name | CIK | Date Filed | File Name`, for example:
  `4    3M CO    66740    20261009    edgar/data/66740/0000066740-26-000293.txt`
  A JSON directory listing is available at `.../daily-index/2026/QTR4/index.json`.
- **Quarterly full index:** `https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/form.idx` (47 MB). Use it for backfill.
- Live form counts for **2026 Q3**: Form 4 = **69,803** (about 1,100 per business day), 4/A = 1,334, Form 3 = 8,042, Form 5 = 72, 8-K = 15,273, 10-Q = 5,416, 10-K = 352, 20-F = 108, 40-F = 4, 6-K = 6,913, 13F-HR = 9,442, Schedule 13D = 2,494 (the form is now named `SCHEDULE 13D`, not `SC 13D`), Form 144 = 17,948.
- **Form 4 retrieval:** each `.txt` path in the index is the complete submission, with the ownership XML embedded between `<XML>` tags, so it costs **1 request per filing**. The XML file name varies (`form4.xml`, `wk-form4_*.xml`, ...), so do not guess it. A per-filing directory listing is at `/Archives/edgar/data/{cik}/{acc-no-dashes}/index.json`.
- Near-real-time Atom feed (optional): `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&count=100&output=atom`.
- Avoid `/Archives/edgar/Feed/YYYY/QTRn/YYYYMMDD.nc.tar.gz`: it is the complete daily feed at **1.56 GB per day**.

### 1.7 Form 4 XML structure (verified, schema `X0609`)
```xml
<ownershipDocument>
  <schemaVersion>X0609</schemaVersion><documentType>4</documentType><periodOfReport>2026-10-06</periodOfReport>
  <issuer><issuerCik>0000320193</issuerCik><issuerName>Apple Inc.</issuerName><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <reportingOwner><reportingOwnerId><rptOwnerCik>0001780525</rptOwnerCik><rptOwnerName>Newstead Jennifer</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>false</isDirector><isOfficer>true</isOfficer><isTenPercentOwner>false</isTenPercentOwner>
      <officerTitle>SVP, GC and Government Affairs</officerTitle></reportingOwnerRelationship></reportingOwner>
  <aff10b5One>true</aff10b5One>                      <!-- 10b5-1 plan flag (new since 2023 rules) -->
  <nonDerivativeTable><nonDerivativeTransaction>
     <securityTitle><value>Common Stock</value></securityTitle><transactionDate><value>2026-10-06</value></transactionDate>
     <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>S</transactionCode></transactionCoding>
     <transactionAmounts><transactionShares><value>2399</value></transactionShares><transactionPricePerShare><value>332.01</value></transactionPricePerShare>
       <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
     <postTransactionAmounts><sharesOwnedFollowingTransaction>...</sharesOwnedFollowingTransaction></postTransactionAmounts>
     <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
  </nonDerivativeTransaction></nonDerivativeTable>
  <derivativeTable>...</derivativeTable><footnotes>...</footnotes>
</ownershipDocument>
```
Transaction codes: P = open-market buy, S = sale, A = grant, M = option exercise, F = tax withholding, G = gift, C = conversion, X = exercise of in-the-money derivative, and others. **For anomaly models, P and S are the signal; the rest is mostly noise.** Prices may sit in footnotes (weighted averages), and values may be absent and replaced by `<footnoteId>`.

**Important change for 2026: the Holding Foreign Insiders Accountable Act (HFIAA).** It was effective 2025-12-18 and requires directors and officers of **foreign private issuers** to file Section 16 Forms 3/4/5. On 2026-03-05 the SEC exempted insiders in **Canada, Chile, the EEA, Korea, Switzerland and the UK**. Verified on EDGAR: in 2026 **Petrobras has 32 Form 4s and 24 Form 3s, Nu Holdings 28 and 15**, while RBC has essentially none. **EDGAR therefore now provides Form 4 data for Brazilian, Mexican, Argentine, Colombian, Peruvian (and Japanese, Chinese, etc.) ADR issuers.** See the Bennett Jones note in the Sources.

### 1.8 SEC structured bulk datasets (DERA / Data Standards)
| Dataset | URL pattern (verified) | Cadence | Notes |
|---|---|---|---|
| **Insider Transactions Data Sets** (Forms 3/4/5 as TSV) | `https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q3_form345.zip` (8.7 MB) | Quarterly, posted around 1 week after quarter end | Tables: SUBMISSION (now with AFF10B5ONE), REPORTINGOWNER, NONDERIV_TRANS, NONDERIV_HOLDING, DERIV_TRANS, DERIV_HOLDING, FOOTNOTES, OWNER_SIGNATURE. History from 2006Q1. **Best for backfill.** |
| Financial Statement Data Sets | `https://www.sec.gov/files/dera/data/financial-statement-data-sets/2026q3.zip` (65 MB) | Quarterly | `sub` (has `accepted` timestamp, `filed`, `cik`, `form`, `period`, `fy`, `fp`), `num`, `tag`, `pre`. From 2009Q2 |
| Financial Statement & Notes | `https://www.sec.gov/files/dera/data/financial-statement-notes-data-sets/2026_09_notes.zip` | **Monthly** | Adds text blocks and notes and dimensional data (larger) |
| Form 13F Data Sets | `https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip` (100 MB) | Quarterly window (Mar–May, Jun–Aug, Sep–Nov, Dec–Feb) | SUBMISSION, COVERPAGE, SUMMARYPAGE, OTHERMANAGER(2), SIGNATURE, **INFOTABLE** (CUSIP, value, shares, put/call, discretion, voting) |
| Fails-to-Deliver | `https://www.sec.gov/files/data/fails-deliver-data/cnsfails202609a.zip` (1.1 MB) | Twice monthly: "a" (1st half) posts around month-end, "b" around the 15th of the next month | Pipe-delimited: `SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE` |

**Pitfall (verified):** the SEC is migrating URLs from `/files/structureddata/...` to `/files/datastandardsinnovation/...`. The new `2026q3_form345.zip` returns **404 at the old path**, while older quarters remain at the old path. **Scrape the hrefs from the landing pages** instead of hard-coding:
- <https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets>
- <https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets>
- <https://www.sec.gov/data-research/sec-markets-data/financial-statement-notes-data-sets>
- <https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets>
- <https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data>

### 1.9 Filing text (8-K, 10-K, 10-Q, 20-F)
- The document itself is at `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/{primaryDocument}` (from submissions). The complete submission is `{accession}.txt`.
- For "Lazy Prices" text similarity, fetch the 10-K/10-Q primary document (HTML or iXBRL) and strip the tags. 8-K item codes come from submissions `items`.
- **EDGAR full-text search** (2001+, JSON, Elasticsearch style, undocumented but used by sec.gov itself): `https://efts.sec.gov/LATEST/search-index?q="going concern"&forms=10-K&dateRange=custom&startdt=2026-09-01&enddt=2026-10-09`. Verified 200. It returns `hits[]._source` with `ciks, form, file_date, adsh, period_ending, sics, biz_locations`, at most 100 hits per page, paged with `from=`.
- Massive also has parsed-filing endpoints: `/stocks/filings/10-K/vX/sections`, `/stocks/filings/8-K/vX/text`. Which plans include them is unverified.

### 1.10 Python libraries (all support Python 3.13)
| Lib | Version (PyPI, 2026-10) | Use |
|---|---|---|
| `httpx` / `aiohttp` + own rate limiter | 0.28.1 | **Recommended**: thin custom client (8 req/s, UA, retry on 429/503) |
| `edgartools` | 5.61.1 (2026-10-06), Py 3.10–3.14, MIT | Form 4 / 13F / XBRL statement parsing; good for prototyping and validating our parser |
| `sec-edgar-downloader` | 5.1.0 | Bulk document download |
| `lxml` | 6.1.3 | Form 4 XML / FIRDS XML streaming (`iterparse`) |
| `arelle-release` / `py-xbrl` | 2.46.1 / 3.0.3 | iXBRL/ESEF parsing when no xBRL-JSON is available |

---

## 2. United States: FINRA short interest, short volume, fails

| Source | Endpoint (verified) | Cadence / latency | Fields |
|---|---|---|---|
| **Short interest file** (exchange-listed + OTC since June 2021; OTC-only before) | `https://cdn.finra.org/equity/otcmarket/biweekly/shrt20260915.csv` (2.7 MB, pipe-delimited with quoted headers) | Twice monthly (settlement mid-month and end of month). Published about 7–8 business days after settlement. Archive back to 2014 | `accountingYearMonthNumber, symbolCode, issueName, issuerServicesGroupExchangeCode, marketClassCode, currentShortPositionQuantity, previousShortPositionQuantity, stockSplitFlag, averageDailyVolumeQuantity, daysToCoverQuantity, revisionFlag, changePercent, changePreviousNumber, settlementDate` |
| **Short interest API** (no key) | `POST https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest` with body `{"limit":5000,"dateRangeFilters":[{"fieldName":"settlementDate","startDate":"2026-09-01","endDate":"2026-10-10"}]}`. Send `Accept: application/json` or `text/plain` for CSV | About 22.6k rows per settlement date. Total row count is in the `record-total` header. Page with `offset` (max 5,000 per call) | same as the file |
| **Reg SHO daily short-sale volume** | `https://cdn.finra.org/equity/regsho/daily/CNMSshvol20261009.txt` | Daily, same evening | `Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market` |
| SEC FTD | see §1.8 | twice monthly | |

- PIT: short interest becomes public on its **publication date**, not the settlement date. Store `knowledge_date` = first-seen date (or settlement date + 8 business days).
- Docs: <https://www.finra.org/finra-data/browse-catalog/equity-short-interest/files>, API PDF <https://www.finra.org/sites/default/files/Equity_Short_Interest_Data_File_Download_API.pdf>.

---

## 3. Massive (formerly Polygon.io)

**Status:** Polygon.io rebranded to **Massive** (massive.com) on **2025-10-30**. Accounts, keys and endpoints are unchanged. **Both `https://api.polygon.io` and `https://api.massive.com` are live and answer identically.** Verified unauthenticated: both return `401 {"status":"ERROR","error":"API Key was not provided"}`. Use `api.massive.com` as the default and keep the host configurable.

- **Auth:** `?apiKey=KEY` query param **or** `Authorization: Bearer KEY`. Env var: **`Polygon_APIKEY`** (existing). Prefer the Bearer header so the key never lands in URLs or logs.
- **Live test:** `.env` is **empty** (0 bytes), so **no live Massive calls were made**. Run the smoke test below once the key is filled in.
- Docs: <https://massive.com/docs/rest/stocks/overview>, LLM index <https://massive.com/docs/llms.txt>, pricing <https://massive.com/pricing>.
- Python client: **`massive`** 2.8.0 (the renamed `polygon-api-client`, which is frozen at 1.16.3), Py 3.9–3.14. A plain `httpx` client is simpler for our needs.

### 3.1 Plans (docs, 2026-10)
| Plan | Price | Rate | History | Freshness | WebSocket | Flat files | Financials & ratios |
|---|---|---|---|---|---|---|---|
| **Stocks Basic (free)** | $0 | **5 calls/min** | **2 years** | End of day | No | No | No |
| Starter | $29/mo | unlimited | 5 y | 15-min delayed | Yes | Yes | No |
| Developer | $79/mo | unlimited | 10 y | 15-min delayed | Yes | Yes | No |
| Advanced | $199/mo | unlimited | 20+ y | real time | Yes | Yes | **Yes** |

### 3.2 Endpoints and free-tier availability (per the docs pages)
| Endpoint | Free (Basic)? | Notes |
|---|---|---|
| `GET /v2/aggs/grouped/locale/us/market/stocks/{YYYY-MM-DD}?adjusted=true&include_otc=false` | **Yes** | **Whole US market for one day in one call.** Fields `T,o,h,l,c,v,vw,t,n`. The key primitive for the free tier |
| `GET /v2/aggs/ticker/{T}/range/1/day/{from}/{to}?adjusted=true&limit=50000` | Yes (2 y) | Per ticker. Too expensive at 5/min for 4k tickers, so use only for spot repairs |
| `GET /v3/reference/tickers?market=stocks&active=true&limit=1000` | Yes | Paginated via `next_url` (each page costs 1 call). Gives `ticker, name, primary_exchange (MIC), type (CS/ADRC/ETF…), cik, composite_figi, share_class_figi, delisted_utc` |
| `GET /v3/reference/tickers/{T}` (Ticker Overview) | Yes | `sic_code, sic_description, market_cap, share_class_shares_outstanding, weighted_shares_outstanding, address, cik, composite_figi, share_class_figi, list_date, primary_exchange, homepage_url, total_employees`. `?date=` gives a historical snapshot (back to 2003). At 5/min, 4,000 tickers take **13.3 hours**, so rotate about 300 per night |
| `GET /stocks/v1/dividends` (new) and `/v3/reference/dividends` (legacy) | Yes (2 y; all history on paid plans) | `ex_dividend_date, record_date, pay_date, declaration_date, cash_amount, currency, frequency, distribution_type, historical_adjustment_factor`. Max limit 5000. Query by `ex_dividend_date.gte` for the whole market in a few calls |
| `GET /stocks/v1/splits` (new) and `/v3/reference/splits` | Yes | Market-wide by date |
| `GET /v2/reference/news?ticker=&published_utc.gte=&limit=1000` | **Yes** (2 y), updated hourly | Includes **`insights[]: {ticker, sentiment (positive/negative/neutral), sentiment_reasoning}`**, plus `tickers[]`, `title`, `description`, `article_url`, `publisher` |
| `GET /stocks/v1/short-interest`, `/stocks/v1/short-volume` | Yes (2 y) | Same data as FINRA, which is free and unlimited, so prefer FINRA |
| `GET /stocks/filings/vX/form-4` | **Yes per docs** ("all individual plans", no history limit) | Parsed Form 4 data (`transaction_code`, owner, price). Useful to cross-check our SEC parser |
| `GET /stocks/financials/v1/{income-statements,balance-sheets,cash-flow-statements,ratios}` | **No: Advanced or the "Financials & Ratios" add-on** | Has `filing_date`, `period_end`, `cik`, `tickers`; history back to 2009. The legacy `/vX/reference/financials` used to be on all plans, but its current status is unverified. **Use SEC companyfacts instead (free, better).** |
| `/stocks/vX/float`, `/v1/related-companies/{T}`, `/vX/reference/tickers/{id}/events` (ticker changes) | Docs list them. Free-tier availability not confirmed | `events` is valuable for ticker-change history in the security master |
| WebSocket | **No** on Basic | |
| Flat files (S3 day aggregates since 2003) | **No** on Basic | Would be the best backfill on Starter+ |

### 3.3 Smoke test to run once the key is present (≤4 calls, 15 s apart, never log the key)
```python
import os, time, httpx
from dotenv import load_dotenv; load_dotenv()   # pip install python-dotenv
H = {"Authorization": f"Bearer {os.environ['Polygon_APIKEY']}"}
B = "https://api.massive.com"
for path in ["/v2/aggs/grouped/locale/us/market/stocks/2026-10-09?adjusted=true",
             "/v3/reference/tickers/AAPL",
             "/v2/reference/news?ticker=AAPL&limit=1",
             "/stocks/financials/v1/income-statements?tickers=AAPL&limit=1"]:   # expect 403 NOT_AUTHORIZED on Basic
    r = httpx.get(B + path, headers=H, timeout=30)
    print(path.split("?")[0], r.status_code, r.json().get("resultsCount", r.json().get("status")))
    time.sleep(15)
```
Pitfalls: grouped-daily includes warrants, units, rights and preferreds (filter with the reference `type` field). Ticker symbols use `.` for classes (`BRK.B`). The 2-year history cap means older history must come from yfinance. A 429 is returned when you exceed 5/min, so the scheduler must serialize Massive calls globally (one token bucket at 5/min, safely 4/min).

---

## 4. yfinance (Yahoo Finance, unofficial) and Stooq

### 4.1 Status
- **yfinance 1.7.0** (released 2026-08-26), PyPI classifiers through Python 3.13. **Installed and tested on Python 3.13.7: works.** It depends on **`curl_cffi`** (0.16.3) to impersonate a browser TLS fingerprint. yfinance creates the session itself; if you pass a session it **must be a `curl_cffi.requests.Session(impersonate="chrome")`**, because plain `requests` and `requests_cache` sessions get blocked or rejected.
- Rate limiting: Yahoo throttles by IP (`YFRateLimitError: Too Many Requests`), usually after a few hundred to a thousand rapid *per-ticker* calls (`.info`, statements). Batch price downloads are cheap. **Benchmarks from this machine:** `yf.download(500 tickers, period="5d")` took **13.4 s, 500/500 OK**; `yf.download(200 tickers, period="max")` took **28.4 s, 200/200 OK**, with no throttling. Recommended: price batches of 200–500 tickers; for fundamentals, at most about 1 ticker/s with a 30–60 s pause every 100 tickers and exponential backoff on `YFRateLimitError`.
- ToS: Yahoo data is "for personal use only". That is acceptable for a local single-user tool, but **do not redistribute**. The library may break without notice when Yahoo changes endpoints, so pin the version and keep a canary test.

### 4.2 History depth (verified, `period="max"`, daily)
AAPL from 1980-12-12. SHEL.L 1996. MC.PA 2000. ASML.AS 1998. SAP.DE 1998. ENI.MI 1995. NESN.SW 1990. 7203.T 1999. 005930.KS 2000. 600519.SS 2001. 0700.HK 2004. PETR4.SA 2000. WALMEX.MX 2003. GGAL.BA 2000. SQM-B.SN 2000. RY.TO 1995. BHP.AX 1988. Typically **about 25 years for EU/LATAM and longer for US**. Use `auto_adjust=False` to keep both `Close` and `Adj Close`, and store raw prices plus actions.

### 4.3 Ticker suffixes (verified live unless marked)
| Market | Suffix | Example (verified) |
|---|---|---|
| US (NYSE/Nasdaq/NYSE American) | none; classes use `-` | `AAPL`, `BRK-B` |
| Toronto / TSX Venture / CSE / Cboe Canada (NEO) | `.TO` / `.V` / `.CN` / `.NE` | `RY.TO`, `GIGA.V`, `PKK.CN`; `.NE` only returns 1d/5d periods |
| Mexico BMV | `.MX` | `WALMEX.MX`, `AMZN.MX` |
| Brazil B3 | `.SA` | `PETR4.SA` |
| Argentina BYMA | `.BA` | `GGAL.BA` |
| Chile Santiago | `.SN` | `SQM-B.SN` |
| Colombia BVC | `.CL` | `ECOPETROL.CL` |
| Peru BVL | `.LM` | `BVN.LM`, `CREDITC1.LM` |
| London LSE / IOB (international order book) | `.L` / `.IL` | `SHEL.L`, `0R2V.IL` |
| Euronext Paris / Amsterdam / Brussels / **Lisbon** / Dublin / Oslo | `.PA` / `.AS` / `.BR` / **`.LS`** / `.IR` / `.OL` | `MC.PA`, `ASML.AS`, `ABI.BR`, `EDP.LS`, `GALP.LS`, `RYA.IR`, `EQNR.OL` |
| Xetra / Frankfurt (others: `.SG .BE .DU .HM .HA .MU`) | `.DE` / `.F` | `SAP.DE`, `SAP.F` |
| Borsa Italiana | `.MI` | `ENI.MI` |
| BME Madrid | `.MC` | `SAN.MC` |
| SIX Swiss | `.SW` | `NESN.SW` |
| Vienna | `.VI` | `OMV.VI` |
| Stockholm / Copenhagen / Helsinki / Iceland | `.ST` / `.CO` / `.HE` / `.IC` | `VOLV-B.ST`, `NOVO-B.CO`, `NOKIA.HE`, `ARION.IC` |
| Baltics: Tallinn / Riga / Vilnius | `.TL` / `.RG` / `.VS` | `TAL1T.TL` (`.RG`/`.VS` not tested) |
| Warsaw / Prague / Budapest / Athens / Istanbul | `.WA` / `.PR` / `.BD` / `.AT` / `.IS` | `PKO.WA`, `CEZ.PR`, `OTP.BD`, `ETE.AT`, `THYAO.IS` |
| Tokyo | `.T` | `7203.T` |
| Korea KOSPI / KOSDAQ | `.KS` / `.KQ` | `005930.KS`, `086520.KQ` (note: Kakao 035720 is now `.KS`, and `.KQ` returns 404) |
| Shanghai / Shenzhen | `.SS` / `.SZ` | `600519.SS`, `000858.SZ` |
| Hong Kong | `.HK` (4-digit, zero-padded) | `0700.HK` |
| Taiwan / Singapore / Australia / NZ / India NSE/BSE | `.TW` (`.TWO` OTC) / `.SI` / `.AX` / `.NZ` / `.NS` / `.BO` | all verified |
| Jakarta / Bangkok / Kuala Lumpur / Johannesburg / Saudi / Tel Aviv | `.JK` / `.BK` / `.KL` / `.JO` / `.SR` / `.TA` | all verified |

Nordic share classes use `-` (`ERIC-B.ST`). Chilean series also use `-` (`SQM-B.SN`).

### 4.4 Fundamentals and info (verified for EDP.LS and AAPL)
- `income_stmt` / `balance_sheet` / `cashflow`: **4 annual periods** (sometimes 5, e.g. EDP cashflow back to 2021) with about 40–90 line items. The `quarterly_*` variants give **5–6 quarters**. `ttm_income_stmt` is also available. Depth is shallow, so this is for current snapshots only.
- `.info` (176–187 keys): `longName, sector, industry, sectorKey, industryKey, country, exchange, fullExchangeName, currency, financialCurrency, marketCap, sharesOutstanding, floatShares, trailingPE, heldPercentInsiders, fullTimeEmployees, firstTradeDateMilliseconds, quoteType`, and more.
- `.isin`: **works for US (`US0378331005`) but returned `-` for EDP.LS.** Do not rely on it, and get ISINs from FIRDS or OpenFIGI.
- `.dividends`, `.splits`, `.earnings_dates` (25 rows) work. `.insider_transactions` is **US only** (empty for EDP). `.news` **returned 0 items** in this test (unreliable).
- **No point-in-time data.** Statements are "as currently reported" and have no filing date. Store `knowledge_date = ingestion date` and never backfill history from yfinance fundamentals into PIT tables.

### 4.5 Stooq: degraded, not recommended
- `https://stooq.com/q/d/l/?s=aapl.us&i=d` now returns an **HTML JavaScript bot challenge** ("This site requires JavaScript to verify your browser"), not CSV. `/q/l/` returns 404 and `/db/h/` (bulk) is behind the same wall. Since around March 2026 Stooq also **requires an API key obtained through a captcha flow** (`https://stooq.com/q/d/?s=spy.us&get_apikey`), and `pandas-datareader`'s Stooq reader is broken (issue #1012).
- Conclusion: **drop Stooq as an automated fallback.** For LATAM, the official fallback is B3 COTAHIST (§8.1).

---

## 5. European Union

### 5.1 ESEF annual reports: filings.xbrl.org (XBRL International)
- Base: `https://filings.xbrl.org/api/` (JSON:API, `application/vnd.api+json`). Docs: <https://filings.xbrl.org/docs/api>. **No key and no documented rate limit**, but they reserve the right to add one, so be polite (≤1 req/s). Free; no formal licence. The underlying reports are public regulatory filings.
- Resources: `/api/filings`, `/api/entities`, `/api/filings/{id}/validation_messages`. Filters use `filter[attr]=value`. Paging uses `page[size]` / `page[number]`. Sorting uses `sort=-date_added`. `include=entity` embeds the issuer.
- Sample: `GET https://filings.xbrl.org/api/filings?filter[country]=PT&sort=-period_end&include=entity&page[size]=3`
  ```json
  {"type":"filing","id":"...","attributes":{"fxo_id":"5493004DM8FGIY6QKF37-2024-12-31-ESEF-PT-0","country":"PT",
   "period_end":"2024-12-31","date_added":"2025-05-08 11:39:05","processed":"...","error_count":0,
   "json_url":"/5493004DM8FGIY6QKF37/2024-12-31/ESEF/PT/0/023002-2024-12-31.json",
   "report_url":".../023002-2024-12-31.html","viewer_url":".../ixbrlviewer.html","package_url":null,"sha256":"..."},
   "relationships":{"entity":{"data":{"type":"entity","id":"..."}}}}
  included: {"type":"entity","attributes":{"name":"NOS, SGPS, S.A.","identifier":"5493004DM8FGIY6QKF37"}}   <- identifier = LEI
  ```
- **xBRL-JSON is available** at `https://filings.xbrl.org{json_url}` in the OIM `https://xbrl.org/2021/xbrl-json` format. Facts look like `{"value":"1093584000.0","decimals":-3,"dimensions":{"concept":"ifrs-full:PropertyPlantAndEquipment","entity":"scheme:<LEI>","period":"2024-01-01T00:00:00","unit":"iso4217:EUR"}}`. NOS 2024 had 678 facts. This makes parsing as easy as SEC companyfacts.
- Entity lookup by LEI: `GET /api/entities?filter[identifier]=529900CLC3WDMGI9VH80` returns EDP, S.A.
- **Coverage (live counts, all years):** GB 2,959 · DK 2,210 · SE 1,415 · FR 1,179 · FI 1,170 · NO 958 · PL 877 · IT 873 · BE 709 · NL 657 · AT 601 · ES 542 · LU 266 · GR 189 · PT 128 · **DE 0 · IE 0**. Total filings in the index: 26,009, which also includes Ukrainian UAIFRS.
- **FY2025 (Dec year-end) coverage as of today:** FR 152, GB 298, DK 261, NL 106, IT 118, but **ES 0, PT 0, SE 0, PL 0**. Some countries have not been refreshed since 2023–2025. **Coverage is patchy and lags.** Treat this as a bonus source, not a primary one.
- **PIT pitfall:** `date_added` is when the repository ingested the filing, **not** the publication date (it is later than publication, so it is conservative). Use `knowledge_date = max(date_added, OAM publication date if known)`.
- Germany: ESEF reports sit in the **Unternehmensregister** (no API, captcha), and Ireland has none here either. Use yfinance fundamentals or company IR sites for those countries.
- **ESAP (European Single Access Point)** opens **July 2027**. Phase 1 covers Transparency Directive documents, including ESEF annual financial reports, plus the Prospectus Regulation and Short Selling Regulation. Revisit then: it should become the EU-wide primary source.
- Libraries: `httpx` plus our own xBRL-JSON parser. Use `arelle-release` 2.46.1 (Py 3.13 OK) to convert raw ESEF zips when `json_url` is missing.

### 5.2 ESMA FIRDS (instrument reference data): bulk, no key
- File list (Solr, JSON): `GET https://registers.esma.europa.eu/solr/esma_registers_firds_files/select?q=*&fq=publication_date:[2026-10-01T00:00:00Z TO 2026-10-10T23:59:59Z]&wt=json&rows=200` returns `{file_name, file_type, publication_date, download_link, checksum}`.
  - Weekly **full files** (published Saturdays): `https://firds.esma.europa.eu/firds/FULINS_E_20261010_01of02.zip` (8.4 MB zip, **365 MB XML**), with a `_02of02` second part. `E` = equities. Other letters cover other CFI classes (`C` = CIVs/ETFs, `D` = debt, ...).
  - **Daily deltas:** `DLTINS_YYYYMMDD_NNofMM.zip`. Daily cancellations: `FULCAN`.
- Record (ISO 20022 `RefData`, verified):
  ```xml
  <RefData><FinInstrmGnlAttrbts><Id>NL00150001S5</Id><FullNm>Kingfish Co NV/The SHRS</FullNm><ShrtNm>KINGFISH ZEELAN/SH VTG FPD</ShrtNm>
    <ClssfctnTp>ECVUFB</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
    <Issr>9845004WD3997B9F1061</Issr>                               <!-- issuer LEI -->
    <TradgVnRltdAttrbts><Id>EBLX</Id><FrstTradDt>2022-11-11T07:30:00Z</FrstTradDt></TradgVnRltdAttrbts>   <!-- MIC -->
    <TechAttrbts><RlvntCmptntAuthrty>NO</RlvntCmptntAuthrty><PblctnPrd><FrDt>2025-11-18</FrDt></PblctnPrd><RlvntTradgVn>MERK</RlvntTradgVn></TechAttrbts></RefData>
  ```
  There is **one row per (ISIN, trading venue)**, so dedupe by ISIN and keep `RlvntTradgVn` (the most relevant market MIC) plus the list of venues. Equity CFI codes start with `ES` (shares) or `ED` (depositary receipts). Filter out `CI*` (funds) and `EY`/`EM` structured products.
- **Gives ISIN ↔ issuer LEI ↔ MIC ↔ CFI ↔ currency** for every instrument traded on an EU venue. This is the backbone of the EU security master. It has **no tickers and no prices**: get tickers from OpenFIGI.
- Parse with `lxml.etree.iterparse` (streaming; 365 MB per part). UK instruments are in **FCA FIRDS** (data.fca.org.uk, same schema), not ESMA, since Brexit.
- Docs: <https://www.esma.europa.eu/publications-and-data/databases-and-registers> (FIRDS reference data, technical spec).

### 5.3 GLEIF LEI: free, no key
- API: `https://api.gleif.org/api/v1/` (JSON:API). **Limit: 60 requests/min** (not shown in headers, so throttle at about 50/min). Docs: <https://api.gleif.org/docs>.
  - `GET /lei-records/529900CLC3WDMGI9VH80` returns `entity.legalName` = "EDP, S.A.", `jurisdiction` = PT, `status` = ACTIVE, `registration.status` = ISSUED, `bic`, `mic`, plus relationships `direct-parent, ultimate-parent, direct-children (48), ultimate-children, isins`.
  - `GET /lei-records?filter[isin]=PTEDP0AM0009` returns `529900CLC3WDMGI9VH80` (ISIN → LEI).
  - `GET /lei-records/{LEI}/isins` returns 33 ISINs for EDP (shares and bonds).
  - `GET /fuzzycompletions?field=entity.legalName&q=Galp Energia` returns "GALP ENERGIA, S.A." → `213800GG5STMYV487X21` (name-based entity resolution).
- **Bulk (preferred):**
  - **ISIN↔LEI mapping, daily:** `GET https://mapping.gleif.org/api/v2/isin-lei/latest` returns `downloadLink`. Today's file is `isin-lei-20261010T071511.zip`.
  - **Golden Copy** (3 publishes/day): `GET https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest`. Full LEI-CDF 3.1 is **3,458,632 records**: CSV zip 480 MB, JSON zip 883 MB. `.../publishes/lei2/latest.csv` redirects (302) to the zip. Relationship records are at `.../rr/latest.csv`. Intraday and daily **delta files** are available, so download the full file weekly and the deltas nightly.
- Use: entity names, parent/child relationships (group structure for consolidation and for mapping subsidiaries' ESEF filings), and ISIN→LEI joins for FIRDS, NSM, AFM and filings.xbrl.org.
- Python: `pygleif` 2026.7.3 (requires Python ≥3.13), or plain `httpx`.

### 5.4 OpenFIGI: free; a key is strongly recommended
- `POST https://api.openfigi.com/v3/mapping` with body `[{"idType":"ID_ISIN","idValue":"PTEDP0AM0009"},{"idType":"TICKER","idValue":"AAPL","exchCode":"US"}]`. Optional header `X-OPENFIGI-APIKEY`, env var **`OPENFIGI_APIKEY`** (already in `.env.example`).
- Response row (verified): `{"figi":"BBG000BPCKW8","name":"EDP SA","ticker":"EDP","exchCode":"PL","compositeFIGI":"BBG000BPCK94","securityType":"Common Stock","marketSector":"Equity","shareClassFIGI":"BBG001S6CXQ9"}`. One ISIN returned **116 listings** (every venue), so filter by `exchCode` (e.g. `PL` = Lisbon, `LN` = London, `FP` = Paris, `NA` = Amsterdam, `GY` = Xetra, `IM` = Milan, `SM` = Madrid, `US` = US composite) or by `micCode`.
- **Limits (docs, confirmed by headers `ratelimit-policy: 25;w=60` without a key):**

  | | without key | with key |
  |---|---|---|
  | `/v3/mapping` | 25 req/min, **10 jobs/req** (250 IDs/min) | **25 req/6 s, 100 jobs/req** (~25,000 IDs/min) |
  | `/v3/search`, `/v3/filter` | 5 req/min | 20 req/min |
- Use: ISIN → Bloomberg ticker + exchange code, which is then mapped to a Yahoo suffix (table in §4.3). Also US ticker → compositeFIGI to join with Massive.
- Pitfalls: Bloomberg tickers ≠ Yahoo tickers (e.g. `VOLVB SS` → `VOLV-B.ST`, Hong Kong padding), so build a mapping rule table and verify with a yfinance probe.

### 5.5 National PDMR / directors' dealings (MAR Art. 19) registers
| Country | Source | Machine-readable? | Verified access | PIT field | Notes |
|---|---|---|---|---|---|
| **Germany: BaFin** | `https://portal.mvp.bafin.de/database/DealingsInfo/` | **Yes: CSV export** | `GET .../sucheForm.do?emittentName=SAP&d-4000784-e=1&6578706f7274=1` returns `text/csv` (`6578706f7274` is hex for "export" in the displaytag library; `e=1` = CSV). Iterate `emittentName=A..Z` or by issuer | `Datum der Aktivierung` (publication timestamp, e.g. `14.09.2026 14:27:19`) | Columns: `Emittent;Bafin-ID;ISIN;Meldepflichtiger;Position / Status;Art des Instruments;Art des Geschäfts;Durchschnittspreis;Aggregiertes Volumen;Mitteilungsdatum;Datum des Geschäfts;Ort des Geschäfts;Datum der Aktivierung`. German decimal format (`176,64 EUR`, `34.485,73 EUR`). **Best EU source: has ISIN, price, volume and timestamp.** Retention is limited (older notices drop off), so archive nightly |
| **Netherlands: AFM** | Register "Transacties leidinggevenden MAR 19" | **Yes: CSV/XML full export** | `https://www.afm.nl/export.aspx?type=0ee836dc-5520-459d-bcf4-a4a689de6614&format=csv` (670 KB, about 9,500 rows) or `&format=xml` (4.8 MB) | Only `transactiedatum`, so use first-seen date | XML adds `meldingid`, `functie` and **issuer `lei`**. **No price, volume or direction** (that is in the detail page or PDF). The AFM also has open CSV exports for short positions and substantial holdings |
| **France: AMF** | BDIF `https://bdif.amf-france.org` | Partial: JSON metadata plus a **PDF** per declaration | `GET https://bdif.amf-france.org/back/api/v1/informations?typesInformation=DD&from=0&size=100` (undocumented SPA backend). 27,738 DD documents | `datePublication` (`2026-10-09T18:10:03+02:00`) | Gives `numero`, `societes[].raisonSociale` and a `documents[].path` PDF. **Transaction details require PDF parsing** (a fixed AMF template, so parsing is feasible with `pdfplumber`). Undocumented API, so it could change |
| **UK: FCA NSM** | see §6.2 | **Yes: JSON search plus HTML RNS text** | `type_code = DSH` ("Director/PDMR Shareholding"): **808 notices from 2026-09-01 to 2026-10-10** | `publication_date` | Full RNS text (HTML) with LEI. Parse the standard MAR notification table (price, volume, nature) |
| Spain: CNMV | cnmv.es → "Notificación de directivos y personas vinculadas" | **No API**; HTML per issuer (ASP.NET) | not automated | publication date on page | Scraping only. Low priority. Alternative: Spanish large caps' NSM/IR feeds |
| Portugal: CMVM | cmvm.pt "Difusão de informação" → "Transações de dirigentes" | **No API**; HTML/PDF | not automated | | Low priority (about 15 PSI names). Monitor via issuer RSS or company IR |
| Italy (CONSOB), Belgium (FSMA), Nordics | not researched in depth | | | | Sweden's FI "Insynsregistret" has a CSV export (well known, **not tested here**) and is worth adding later |

---

## 6. United Kingdom

### 6.1 Companies House API: free key
- REST: `https://api.company-information.service.gov.uk`. Auth is **HTTP Basic with the API key as username and an empty password**. Without a key you get `401 {"error":"Empty Authorization header"}` (verified). Env var **`COMPANIES_HOUSE_APIKEY`**.
- **Rate limit: 600 requests per 5 minutes** (429 when exceeded). Docs: <https://developer.company-information.service.gov.uk/>.
- Endpoints: `/company/{number}` (profile, SIC codes, status), `/company/{number}/officers`, `/company/{number}/persons-with-significant-control`, `/company/{number}/filing-history?category=accounts`. The document API is `https://document-api.company-information.service.gov.uk/document/{id}/content` with `Accept: application/xhtml+xml` for iXBRL.
- **Bulk (no key):**
  - **Accounts Data Product**: `https://download.companieshouse.gov.uk/en_accountsdata.html`, daily `Accounts_Bulk_Data-2026-10-10.zip` (Tue–Sat; 52–600 MB each). It holds iXBRL/XBRL of **electronically filed** accounts (about 75% of filings; paper filings excluded). Daily files are kept 60 days, then monthly archives.
  - **Basic Company Data**: monthly `BasicCompanyData-2026-10-01-part1_7.zip` (name, number, SIC, status, address).
- Relevance: mostly private and small companies. **Listed PLC group accounts are better taken from the FCA NSM** (§6.2), because many large PLCs' Companies House accounts are PDFs. Use Companies House mainly for SIC codes, officers and PSC data (entity resolution, director networks).

### 6.2 FCA National Storage Mechanism (NSM): no key (undocumented JSON)
- Search API, reverse-engineered from the data.fca.org.uk Angular app (config key `nsm_index_name:"nsm-search"`):
  ```
  POST https://api.data.fca.org.uk/search?index=nsm-search
  {"from":0,"size":1000,"sort":"submitted_date","sortorder":"desc",
   "criteriaObj":{"criteria":[{"name":"latest_flag","value":"Y"},{"name":"type_code","value":["DSH"]}],
                  "dateCriteria":[{"name":"submitted_date","value":{"from":"2026-09-01T00:00:00Z","to":"2026-10-10T23:59:59Z"}}]}}
  ```
  It returns an Elasticsearch response with `hits.total.value` (5,499,580 documents in total) and `_source`: `{submitted_date, publication_date, company, lei, isin, type, type_code, headline, tag_esef, document_format, download_link, source (RNS/PRN/BWI/GNW/EQS/Direct Upload), category_group}`.
- Download: `https://data.fca.org.uk/artefacts/{download_link}`, for example `.../artefacts/NSM/RNS/392c8fe5-....html` (RNS text) or `.../NSM/DirectUpload/NI-000152986/NI-000152986_<LEI>-2026-06-30.zip` (**ESEF/UKSEF report package**, 3.2 MB). Verified 200.
- Useful filters: `type_code` (`DSH` = Director/PDMR Shareholding, `ACS` = Annual Financial Report, `HOL` = Holding(s) in Company, `POS` = Transaction in Own Shares, `IOE` = Issue of Equity, `NOR` = Notice of Results, ...), `tag_esef: ["Tagged"]` (3,021 tagged AFRs), and `lei`. Filtering by `type` name returned 0, so **filter by `type_code`**.
- **This is how to get LSE RNS director dealings for free**, since the RNS feed is mirrored into the NSM. LSE's own RNS pages and API are commercial (paid LSEG data). Investegate.co.uk is a scraping alternative, but the NSM is cleaner.
- PIT: `publication_date` (to the minute). **Caveat:** the API is undocumented, so wrap it in an adapter with a schema check, and keep it ≤1 req/s.
- UK ESEF/UKSEF packages are iXBRL zips with no xBRL-JSON. Convert with Arelle (`arelle-release`, using the OIM xBRL-JSON save plugin), or use filings.xbrl.org's GB copy (2,959 filings, which already has `json_url`).
- Also on the FCA data portal: **FCA FIRDS** (UK instrument reference data, same XML schema as ESMA) and the FCA daily **short positions** spreadsheet (not tested).

---

## 7. Canada

- **SEDAR+** (sedarplus.ca, launched July 2023): **no public API**. Pages sit behind bot protection, and the Terms of Use (2023-04-28) restrict automated collection. Bulk access is only via the CSA **Data Distribution Services (DDS)**, a commercial SFTP feed covering SEDAR+, NRD and SEDI. **Not usable for free.**
- **SEDI** (insider reports, about 22k reports/month): public web search only, no free bulk download. Canadian insiders are **exempt from the US HFIAA Form 4 requirement** (SEC order 2026-03-05), so EDGAR has no Canadian insider data.
- **EDGAR covers most Canadian large caps** (verified most recent annual form):
  - 40-F (MJDS, IFRS XBRL in companyfacts `ifrs-full`): RY, TD, BMO, BNS, CM, CNQ, CNI, SU, MFC, SLF, TRP, NTR, BN, TRI, CVE, AEM, FNV, WPM, TECK, GIB.
  - 10-K (US-GAAP): ENB, CP, SHOP, BAM, WCN, ABX/GOLD, CCO, IMO, QSR, OTEX, LULU.
  - Neither: ATD (Couche-Tard, not SEC-registered).
- Strategy: EDGAR for SEC-registered Canadians (about 150–200 names). yfinance `.TO/.V/.CN` for prices and snapshot fundamentals of the rest. No free Canadian insider source (SEDI scraping is not recommended).

---

## 8. Latin America

### 8.1 Brazil: CVM open data (excellent, no key) and B3
Portal: <https://dados.cvm.gov.br/> (CKAN; API `https://dados.cvm.gov.br/api/3/action/package_list` verified). Files are under `https://dados.cvm.gov.br/dados/CIA_ABERTA/`, are **refreshed daily** (Last-Modified 2026-10-04), use `;` as separator and are **Latin-1** encoded.

| Dataset | URL (verified) | History | Content / key fields |
|---|---|---|---|
| Cadastro | `CAD/DADOS/cad_cia_aberta.csv` (2,678 issuers) | current | `CNPJ_CIA, DENOM_SOCIAL, CD_CVM, SIT, SETOR_ATIV, TP_MERC, CATEG_REG, SIT_EMISSOR, CONTROLE_ACIONARIO, ...` |
| **DFP** (annual statements) | `DOC/DFP/DADOS/dfp_cia_aberta_2025.zip` (12.8 MB) | **2010→** | Files `BPA/BPP` (balance sheet), `DRE` (income), `DFC_MD/MI` (cash flow), `DMPL`, `DVA`, `DRA`, each `_con` (consolidated) and `_ind` (parent-only), plus `composicao_capital` and `parecer`. Row: `CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;GRUPO_DFP;MOEDA;ESCALA_MOEDA(MIL/UNIDADE);ORDEM_EXERC(ÚLTIMO/PENÚLTIMO);DT_INI_EXERC;DT_FIM_EXERC;CD_CONTA;DS_CONTA;VL_CONTA;ST_CONTA_FIXA` |
| **ITR** (quarterly) | `DOC/ITR/DADOS/itr_cia_aberta_2026.zip` | **2011→** | same layout. Index file has `DT_RECEB` (receipt date, **PIT**), `VERSAO` (restatements) and `LINK_DOC` |
| FRE (reference form) | `DOC/FRE/DADOS/fre_cia_aberta_2026.zip` | 2010→ | governance, shareholders, management compensation, capital structure |
| FCA (registration form) | `DOC/FCA/DADOS/fca_cia_aberta_2026.zip` | 2010→ | **`valor_mobiliario`: CNPJ ↔ `Codigo_Negociacao` (B3 ticker, e.g. BBAS3) ↔ segment (Novo Mercado...)**, plus `pais_estrangeiro_negociacao` (ADR listings), auditor, DRI |
| IPE (periodic and event documents) | `DOC/IPE/DADOS/ipe_cia_aberta_2026.zip` | 2003→ | Fatos relevantes, comunicados and AGMs: `Categoria, Tipo, Assunto, Data_Entrega, Link_Download` (PDF). This is the **filings-text source** |
| **VLMO** (insider positions and trades, art. 11 RCVM 44) | `DOC/VLMO/DADOS/vlmo_cia_aberta_2026.zip` | 2018→ | Monthly consolidated: `Tipo_Cargo` (Controlador / Conselho de Administração / Diretor ...), `Tipo_Movimentacao`, `Tipo_Operacao` (Crédito/Débito), `Tipo_Ativo`, `Data_Movimentacao`, `Quantidade`, `Preco_Unitario`, `Volume`. Index has `Data_Entrega`. **Aggregated by role, not by named person.** Filed by the 10th of the following month |

- PIT: `DT_RECEB` / `Data_Entrega` plus `VERSAO`. Keep every version.
- Pitfalls: `ESCALA_MOEDA` is MIL (thousands) or UNIDADE. Account codes `CD_CONTA` differ for banks and insurers (different chart of accounts). The `ÚLTIMO`/`PENÚLTIMO` rows duplicate the prior year. Financial institutions report under IFRS consolidated plus BRGAAP (COSIF), with no XBRL.
- **B3 COTAHIST (official historical quotes, free):** `https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A2025.ZIP` (annual, 89 MB) and **daily `COTAHIST_D09102026.ZIP`** (564 KB), both verified 200. Fixed-width (245 chars; layout PDF on the B3 site). Covers every B3 ticker with OHLC, volume, number of trades and ISIN (`CODISI`). Prices are **unadjusted**, so apply CVM/B3 corporate actions (or compare with yfinance `Adj Close`). This is the **official fallback for Brazil prices.**

### 8.2 Other LATAM regulators
| Country | Regulator / exchange | Machine-readable financials? | Recommendation |
|---|---|---|---|
| Mexico | CNBV, BMV (Emisnet), BIVA | **No free API or bulk.** Emisnet is HTML/PDF/XBRL per filing, behind a web UI | yfinance `.MX` for prices and snapshot fundamentals. EDGAR 20-F for large caps (below) |
| Chile | CMF (cmfchile.cl) | Issuers file IFRS **XBRL (taxonomy CMF CL-CI)**, but downloads are per company through the web portal. `api.cmfchile.cl` **needs a free key** and covers **macro and bank data only** (UF, USD, IPC, bank balance sheets), verified: `422 "API key no ha sido suministrada"` | Optional key (`CMF_APIKEY`) for macro. EDGAR 20-F plus yfinance `.SN` for equities. Chilean insiders are HFIAA-exempt |
| Argentina | CNV, AIF portal (aif2.cnv.gov.ar), BYMA | HTML/PDF portal, no API | EDGAR 20-F (most liquid names are ADRs) plus yfinance `.BA` |
| Colombia | SFC (SIMEV), datos.gov.co (Socrata) | datos.gov.co holds some SFC datasets (mostly financial-sector entities). No issuer XBRL bulk | EDGAR (EC, CIB) plus yfinance `.CL` |
| Peru | SMV (smv.gob.pe) | Issuers file XBRL (SMV taxonomy since 2012), but only via web search. No documented bulk/API | EDGAR (BAP, BVN, IFS) plus yfinance `.LM` |

### 8.3 LATAM large caps on EDGAR (verified most recent annual form, 2026)
- **Brazil (20-F):** PBR, VALE, ITUB, BBD, BSBR, ABEV, GGB, SID, SUZ, EMBJ (Embraer), SBS (Sabesp), CIG (Cemig), UGP, TIMB, VIV, BAK, XP, STNE, PAGS, NU, ARCO. (Eletrobras/AXIA, Copel, BRF/MBRF and Cosan were not found under the tested tickers: verify their current ADR symbols.)
- **Mexico (20-F):** AMX, FMX, KOF, TV, CX, PAC, ASR, OMAB, VLRS, SIM.
- **Chile (20-F):** SQM, BCH, BSAC, LTM, ENIC, CCU (Andina: verify its symbol).
- **Argentina (20-F):** YPF, GGAL, PAM, BMA, BBAR, SUPV, TEO, TGS, EDN, LOMA, CEPU, IRS, CRESY, VIST, GLOB. **MELI files a 10-K** (US domestic filer).
- **Colombia / Peru:** EC, CIB (20-F); BAP, BVN, IFS (20-F); **SCCO files a 10-K**.
- All of these have `ifrs-full` (or `us-gaap`) facts in companyfacts. Since 2026, those not in an exempt country (BR, MX, AR, CO, PE) **also file Forms 3/4** under the HFIAA.

---

## 9. Identifier crosswalk (how the pieces join)

```
US:    ticker ─(SEC company_tickers_exchange)─ CIK ─(submissions.lei, often null)─ LEI
       ticker ─(Massive /v3/reference/tickers)─ composite_figi / share_class_figi / cik
       CUSIP (FTD, 13F) ─(OpenFIGI idType=ID_CUSIP)─ FIGI / ticker
EU:    ISIN ─(ESMA FIRDS)─ issuer LEI, MIC, CFI ─(GLEIF)─ legal name, parent/children
       ISIN ─(OpenFIGI)─ Bloomberg ticker + exchCode → Yahoo symbol (suffix table §4.3)
       LEI ─ filings.xbrl.org entity / FCA NSM `lei` / AFM `lei` / BaFin `ISIN`
UK:    ISIN/LEI ─(FCA FIRDS + NSM)─ RNS ; Companies House number ─(GLEIF registeredAs)─ LEI
BR:    CNPJ ─(CVM FCA valor_mobiliario)─ B3 ticker (PETR4) ─ Yahoo PETR4.SA ; ISIN from COTAHIST CODISI
```
Internal key proposal: `security_id` (our surrogate) keyed by **share-class FIGI** when available, else ISIN+MIC. `issuer_id` keyed by **LEI**, else CIK, else CNPJ. Keep a `symbol_history` table (ticker changes over time) for PIT joins.

---

## 10. Recommended ingestion strategy

### 10.1 Per region and data type
| Region | (a) Security master / IDs | (b) Daily prices | (c) Fundamentals (PIT) | (d) Insider | (e) Filings text |
|---|---|---|---|---|---|
| **US** | SEC `company_tickers_exchange.json` (daily) + Massive `/v3/reference/tickers` (weekly, paginated) + OpenFIGI for CUSIP | **Massive grouped daily** (1 call/day). Backfill: Massive 2 y (≈500 calls ≈ 2 h one-off at 4/min) plus **yfinance `period="max"`** for pre-2024. Fallback: yfinance batch | **SEC companyfacts**: weekly `companyfacts.zip`, nightly per-CIK refetch for CIKs in that day's `form.idx` with 10-K/10-Q/20-F/40-F/6-K/8-K. Cross-check with FSDS quarterly (has `accepted` timestamp) | **SEC Form 3/4/5** nightly from `form.YYYYMMDD.idx` (≈1.1k `.txt` fetches). Backfill: **Insider Transactions Data Sets** 2006→. Optional cross-check: Massive `/stocks/filings/vX/form-4` | EDGAR Archives (primary docs of 10-K/10-Q/8-K for the universe) + EFTS full-text search; 8-K items from submissions |
| **US extras** | | | 13F via quarterly 13F data sets | FINRA short interest (2×/month), Reg SHO daily, SEC FTD (2×/month) | Massive news (with sentiment insights), 1–3 calls/night |
| **EU** | **ESMA FIRDS FULINS_E weekly + DLTINS daily** → ISIN, LEI, MIC; GLEIF ISIN-LEI daily file + Golden Copy weekly; OpenFIGI (key) → ticker → Yahoo symbol | **yfinance** batches (200–500 symbols) | **filings.xbrl.org** xBRL-JSON where available (FR, NL, IT, BE, DK, FI, NO, AT...). Otherwise yfinance snapshot fundamentals (no PIT; `knowledge_date = ingest date`). Germany: yfinance only | **BaFin CSV** (DE), **AFM XML** (NL), **AMF BDIF** JSON+PDF (FR); ES/PT deferred | AMF BDIF docs; ESEF report HTML (management report text) |
| **UK** | FCA FIRDS + NSM `lei` + GLEIF; Companies House for SIC/officers | yfinance `.L` | **FCA NSM ESEF/UKSEF zips** (`type_code=ACS`, `tag_esef=Tagged`) → Arelle → xBRL-JSON; or filings.xbrl.org GB | **FCA NSM `type_code=DSH`** RNS HTML (parse MAR table) | NSM RNS full text (results, trading updates) |
| **Brazil** | CVM `cad_cia_aberta` + FCA `valor_mobiliario` (CNPJ↔ticker) | yfinance `.SA`; fallback **B3 COTAHIST daily** | **CVM DFP/ITR** (daily refresh of current-year zips; PIT `DT_RECEB`) + EDGAR 20-F for ADRs | **CVM VLMO** (monthly, by role) + **EDGAR Form 4** (ADR issuers, 2026+) | **CVM IPE** (fatos relevantes PDFs) |
| **MX / CL / AR / CO / PE** | OpenFIGI + EDGAR (ADRs) | yfinance `.MX .SN .BA .CL .LM` | EDGAR companyfacts for 20-F filers; yfinance for the rest | EDGAR Form 4 for MX/AR/CO/PE ADR issuers (CL exempt) | EDGAR 20-F/6-K text |
| **Canada** | EDGAR + OpenFIGI | yfinance `.TO .V .CN` | EDGAR companyfacts (40-F `ifrs-full` / 10-K); yfinance for the rest | none free (SEDI has no bulk) | EDGAR 40-F/6-K |

### 10.2 Nightly schedule and request budget (about 4,000 US + 3,000 non-US securities)
| Job (start after 23:00 Lisbon so the SEC daily index, around 22:00 ET, is ready; or run at 06:00 Lisbon) | Requests/night | Data volume | Est. runtime |
|---|---|---|---|
| SEC tickers + daily `form.idx` | 2 | 1.4 MB | <5 s |
| SEC Form 3/4/5 (`.txt`) | ~1,100–1,500 | ~10 MB | ~3 min @ 8 req/s |
| SEC submissions + companyfacts refresh for CIKs that filed (filtered to universe) | ~200–1,600 (earnings season peak) | 50–400 MB | 0.5–4 min |
| SEC primary docs (10-K/10-Q/8-K text, universe only) | ~100–600 | 50–300 MB | 0.5–2 min |
| Massive grouped daily (+ dividends/splits by date + news) | **4–5** (must stay ≤5/min) | 2 MB | ~1 min (paced) |
| Massive ticker-details rotation (≈1/14 of universe) | ~280 | small | **~70 min at 4/min**, so run it as a low-priority background lane |
| FINRA Reg SHO daily (+ short interest / FTD when published) | 1–3 | 1–3 MB | <10 s |
| yfinance prices: 7,000 symbols, `period="5d"`, 14–35 batches | ~35 batch calls (internally ~7k chart requests) | 5 MB | **~3–5 min** (measured 500 tickers in 13 s) |
| yfinance fundamentals/info rotation (≈1/7 of 3,000 non-US + US gaps) | ~450 tickers × 4–5 ≈ 2,000 | small | ~20–35 min with pacing |
| ESMA FIRDS DLTINS (daily) [FULINS weekly Sat: 2 files, 17 MB zip / 730 MB XML] | 2–5 | 5–20 MB | 1–3 min parse (weekly full ~5 min) |
| GLEIF ISIN-LEI daily + LEI delta | 2–3 | ~50 MB | 1–2 min |
| OpenFIGI new/changed ISINs only (initial 7k load: 70 calls with key ≈ 20 s; **28 min without key**) | 0–10 | small | <10 s |
| filings.xbrl.org poll (`sort=-date_added`) + new xBRL-JSON | 2–200 | 0–100 MB | <3 min |
| FCA NSM: DSH + ACS/tagged + results queries, plus fetching new docs | ~5 + 20–150 | 5–200 MB | 1–3 min |
| BaFin CSV (26 letter queries, or ISIN list) · AFM XML (1) · AMF BDIF (1–3 pages + 20–80 PDFs) | ~30 + 1 + ~80 | 10 MB | 2–4 min |
| CVM current-year zips (DFP, ITR, IPE, VLMO, FCA, CAD) + B3 COTAHIST daily | 7 | ~60 MB | 1–2 min |
| **Total (excluding background Massive lane)** | **≈ 4,000–7,500 HTTP requests** | 0.3–1.2 GB | **≈ 35–60 min** |

Weekly (Sunday): SEC `companyfacts.zip` (1.41 GB) and `submissions.zip` (1.57 GB), Massive full ticker list (about 12 pages), FIRDS FULINS, GLEIF Golden Copy. Quarterly: SEC FSDS/FSN, Insider TSV, 13F datasets.
One-off backfill: SEC zips (3 GB) + Insider datasets 2006→ (~80 quarters × ~10 MB) + FSDS 2009→ (~70 × 60 MB ≈ 4 GB) + yfinance max history for 7k symbols (about 15–20 min) + CVM 2010→ zips (~1 GB) + B3 COTAHIST annual files (optional, ~90 MB per year).

### 10.3 Engineering rules for adapter builders
1. **One rate limiter per host**, global across threads and processes: `sec.gov`/`data.sec.gov` at 8/s, `api.massive.com` and `api.polygon.io` sharing **4/min** (same account quota), `api.gleif.org` at 50/min, OpenFIGI at 25/min without a key or 25/6 s with one, Companies House at 2/s, filings.xbrl.org and FCA at 1/s, and Yahoo in batches with jittered sleeps.
2. **Point in time:** every fact row carries `knowledge_ts`. Use SEC `acceptanceDateTime`; CVM `DT_RECEB`/`Data_Entrega`; NSM `publication_date`; BaFin `Datum der Aktivierung`; AMF `datePublication`; filings.xbrl.org `date_added`; FINRA publication date (not settlement); and for yfinance, AFM and FIRDS, use first-seen. Never overwrite: append new versions.
3. **Raw zone first:** store raw responses (zip, json, xml, csv) in `data/raw/{source}/{date}/` before parsing, so that parsers can be re-run.
4. Treat undocumented endpoints (FCA NSM `api.data.fca.org.uk`, AMF `bdif.../back/api`, BaFin displaytag export, SEC EFTS) as **fragile**: add schema assertions and alerting, and fail soft.
5. Encoding: CVM is Latin-1 with `;`; BaFin is UTF-8 with BOM, `;` and German number format; AFM XML has US-style dates (`10/8/2026 12:00:00 AM`); FINRA is pipe-delimited with quoted headers.
6. Secrets: read `Polygon_APIKEY`, `OPENFIGI_APIKEY`, `COMPANIES_HOUSE_APIKEY` and `SEC_USER_AGENT` from `.env`. Send keys in **headers**, not query strings, so they do not appear in logs (Massive supports Bearer).

---

## 11. Free API keys to register
| Service | Env var | Signup | Why |
|---|---|---|---|
| Massive (Polygon) | `Polygon_APIKEY` (**`.env` is currently empty, so fill it in**) | <https://massive.com/dashboard/signup> (keys: <https://massive.com/dashboard/keys>) | US grouped daily prices, reference, corporate actions, news sentiment, Form 4 |
| **OpenFIGI** | `OPENFIGI_APIKEY` | <https://www.openfigi.com/user/signup> (then API key in account settings) | 100× higher mapping throughput (100 jobs/req, 25 req/6 s) |
| **Companies House** | `COMPANIES_HOUSE_APIKEY` | <https://developer.company-information.service.gov.uk/> → register, then create an application and a REST key | UK company profiles, officers, PSC, accounts documents |
| CMF Chile (optional) | `CMF_APIKEY` | <https://api.cmfchile.cl/> ("Solicitar API Key") | Chilean macro/bank series only |
| SEC | `SEC_USER_AGENT` (no key) | n/a: just declare `BiG Equity 360 research mecoserra2008@gmail.com` | mandatory UA |
| GLEIF, ESMA FIRDS, filings.xbrl.org, FINRA, CVM, B3, FCA NSM, BaFin, AFM, AMF | none | n/a | no key needed |

Not needed or not useful: Stooq key (captcha plus JavaScript wall, so not automatable); SEDAR+/SEDI (no free API).

---

## 12. Dead, paywalled or limited sources (summary)
| Source | Status |
|---|---|
| Stooq CSV | **Degraded/dead for automation**: JavaScript bot challenge plus captcha-issued API key since around March 2026 |
| Massive financials (`/stocks/financials/v1/*`) | **Paywalled** (Stocks Advanced $199/mo or add-on). Use SEC companyfacts |
| Massive WebSocket / flat files | Starter+ ($29/mo). Not on free tier |
| Massive history | Free tier limited to **2 years** and 5 calls/min |
| SEDAR+ / SEDI | No public API; bulk via paid CSA DDS |
| ESAP (EU single access point) | Not live until **July 2027** |
| filings.xbrl.org | Free but **incomplete** (DE/IE absent; ES/PT/SE/PL lag by more than 1 year) |
| Germany Unternehmensregister | No API, captcha |
| CNMV (ES), CMVM (PT) PDMR | HTML only, no machine-readable export |
| AMF PDMR | Metadata JSON, but details only in PDFs |
| AFM PDMR | Machine-readable list but no price or volume |
| Mexico/Argentina/Colombia/Peru/Chile issuer financials | No free bulk feed. Use EDGAR 20-F for ADRs and yfinance for the rest |
| LSE RNS direct | Commercial. Use the FCA NSM mirror instead (free) |
| yfinance | Works (1.7.0, Py 3.13) but unofficial, personal-use ToS, no PIT fundamentals, `.isin` unreliable outside US, `.news` returned empty |

---

### Sources
- SEC EDGAR APIs: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces> · Developer resources and fair access: <https://www.sec.gov/about/developer-resources>
- SEC datasets: [Insider transactions](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets) · [Financial statement data sets](https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets) · [13F data sets](https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets) · [Fails-to-deliver](https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data)
- HFIAA / FPI Section 16 (Bennett Jones, 2026-03): <https://www.bennettjones.com/Insights/Blogs/2026/03/Canadian-Foreign-Private-Issuers-Remain-Exempt-from-US-Insider-Reporting-Requirements>
- FINRA short interest: <https://www.finra.org/finra-data/browse-catalog/equity-short-interest/files> · API guide: <https://www.finra.org/sites/default/files/Equity_Short_Interest_Data_File_Download_API.pdf>
- Massive: <https://massive.com/pricing> · <https://massive.com/docs/rest/stocks/overview> · <https://massive.com/docs/llms.txt> · rebrand note: <https://apicostcalc.com/blog/polygon-massive-rebrand-api-pricing.html>
- yfinance: <https://pypi.org/project/yfinance/> · rate-limit discussions: <https://github.com/ranaroussi/yfinance/discussions/2431>, <https://github.com/ranaroussi/yfinance/issues/2422>
- Stooq API-key change: <https://github.com/pydata/pandas-datareader/issues/1012>
- filings.xbrl.org API: <https://filings.xbrl.org/docs/api>
- ESAP timing: <https://cyprus-mail.com/2026/07/14/new-eu-financial-platform-moves-closer-to-its-2027-launch-date>, <https://securities-services.societegenerale.com/de/insights/views/news/esap-european-single-access-point-the-platform-will-open-on-july-10-2027>
- GLEIF API: <https://www.gleif.org/en/lei-data/gleif-api> · <https://api.gleif.org/docs> · rate limit via <https://pygleif.readthedocs.io/en/2026.7.3/usage.html>
- OpenFIGI: <https://www.openfigi.com/api/documentation>
- AFM MAR19 register: <https://afm.nl/nl-nl/sector/registers/meldingenregisters/transacties-leidinggevenden-mar19->
- Companies House: <https://developer-specs.company-information.service.gov.uk/guides/rateLimiting> · <https://download.companieshouse.gov.uk/en_accountsdata.html>
- CSA DDS / SEDAR+: <https://www.securities-administrators.ca/?p=33446> · Terms: <https://systems.securities-administrators.ca/?p=4978>
- CMF Chile XBRL taxonomy: <https://www.cmfchile.cl/portal/principal/623/w4-article-16479.html>
- CVM open data: <https://dados.cvm.gov.br/>
