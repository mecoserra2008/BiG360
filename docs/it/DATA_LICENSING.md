# Data licensing matrix and vendor adapter

**Status:** 2026-10-20.
**Scope:** every external source the code fetches. The inventory comes from `backend/app/ingestion`, `app/macro`, `app/realtime/streams`, `app/earnings`, `app/linkage`, `app/nlp`, `contracts/jobs.yaml`, `coverage.yaml` and `macro_catalog.yaml`.

> **This is an engineering assessment, not legal advice.** BiG Legal / Compliance and the market-data team must confirm each "OK" before the app is used beyond one analyst's research. Terms change; re-check the URLs at review time. "Commercial use" here means use inside a bank: displayed to several employees, feeding internal systems or decisions. External redistribution of data is never assumed to be allowed.

**Legend**
- **Type:** OFF = government, regulator, central bank or international organisation. EXCH = exchange website. NPO = non-profit or open project. COMM = commercial vendor. UNOFF = unofficial scraper or library against a site that does not offer the data for automated reuse.
- **Bank use:**
  - ✅ OK with attribution.
  - ⚠️ review: plan-dependent, terms unclear, or allowed only under conditions.
  - ⛔ not licensed for this use; replace or switch off.

---

## 1. Summary

| Area | Status | Action before bank use |
|---|---|---|
| US filings, insiders, 13F, FTD (SEC EDGAR) | ✅ | Set `SEC_USER_AGENT` to a bank contact mailbox. Respect 10 requests per second. |
| EU/UK/JP/KR/BR/LATAM regulators and ESEF filings | ✅ / ⚠️ | Attribution notice. Review the few undocumented endpoints (§2.2). |
| Macro: FRED, ECB, Eurostat, OECD, IMF, World Bank, BIS, national statistics offices and central banks | ✅ | Attribution notice. **Remove the ICE BofA OAS archive** unless the bank licenses ICE data (§2.4). |
| Rates & debt (W3): Treasury, Fiscal Data, ECB, Bundesbank, BoE, MoF, BdE, BPstat, SNB, BoC, RBA, Riksbank, Norges Bank, NY Fed, DMO, ESMA FIRDS, OpenFIGI | ✅ / ⚠️ | Attribution. EURIBOR (EMMI), ICE BofA and Moody's series are personal use (§2.8). FINRA API only with keys. |
| CFTC Commitments of Traders, EU/UK short registers | ✅ | Attribution. |
| GDELT | ✅ | Citation. |
| Common Crawl CC-NEWS (N1 historical news) | ⚠️ derived metadata only | Common Crawl Terms of Use apply; publishers keep the copyright of their articles. Only derived metadata is stored (no article text). Review with legal before bank use. |
| **Yahoo Finance (yfinance): global prices, benchmarks, futures curves, earnings dates, snapshot fundamentals** | ⛔ | **Replace with a licensed vendor (§4) or the bank's market-data service.** Switch the yfinance jobs off in the bank deployment. |
| **Massive (Polygon.io): US prices and news** | ⚠️ plan-dependent | Use a business plan with the exchange (CTA/UTP) agreements needed for internal display, or replace with the bank's feed. |
| AKShare, BaoStock (China), Google News RSS, mindicador.cl | ⛔ | Switch off. Replace with a vendor (China A-shares) or the official source (Chile: BCCh SieteRestWS). |
| Exchange websites (TDnet, HKEX, KRX, JPX, SSE/SZSE, Nasdaq Nordic, Oslo, B3) | ⚠️ | Mostly personal-use website terms. Use the bank's exchange data licences or a vendor. |
| NLP models (Hugging Face), Loughran-McDonald word list | ⚠️ | Confirm the model licences. Use the built-in word subsets or buy the LM commercial licence. |

## 2. Licence matrix

### 2.1 Prices and market data

| Source (source_id) | Used for | Type | Terms | Bank use | Replacement in a bank deployment |
|---|---|---|---|---|---|
| Massive, formerly Polygon.io (`polygon`, `polygon_news`); key `POLYGON_APIKEY` | US end-of-day grouped bars, splits and dividends, reference tickers, US news | COMM | https://polygon.io/terms (Massive) | ⚠️ The free / individual plans are for non-professional, personal use. Internal multi-user display needs a business plan, and exchange redistribution rules (CTA/UTP) apply. | Bank feed (Bloomberg B-PIPE / Data License, LSEG, FactSet Prices) or a Massive business plan |
| Yahoo Finance via `yfinance` (`yfinance`, `yahoo`, benchmarks `IDX:` / `ETF:`) | Non-US daily prices (EU, UK, JP, HK, CA, LATAM), splits and dividends, index / ETF / FX / commodity benchmarks, futures term structures, market cap, earnings dates, snapshot fundamentals | UNOFF | https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html. yfinance itself says it is "intended for research and educational purposes" and is not affiliated with Yahoo. | ⛔ **Not licensed for commercial use**; personal use only, no redistribution | Licensed vendor (§4), the bank's market-data service, official exchange end-of-day files under an exchange licence |
| B3 COTAHIST (`b3`) | Brazil historical quotes | EXCH | https://www.b3.com.br/en_us/market-data-and-indices/ (market-data policy) | ⚠️ Free download; commercial use falls under the B3 market-data policy | Vendor or B3 licence |
| KRX Open API (`krx`); key `KRX_OPENAPI_KEY` | Korea daily bars | EXCH | https://openapi.krx.co.kr (terms of the service) | ⚠️ Check the service terms for internal commercial use | Vendor |
| TWSE / TPEx OpenAPI (`twse`, `tpex`) | Taiwan quotes, ex-rights tables | EXCH / OFF | https://data.gov.tw/license (Open Government Data License, Taiwan) | ✅ with attribution (OpenAPI datasets) | Vendor for real-time |
| BaoStock (`baostock`) | China A-share daily bars, PE/PB, fundamentals | UNOFF | http://baostock.com (free, personal research) | ⛔ | Vendor (Wind / FactSet / LSEG for China A) |
| ECB euro reference rates (`ecb`) | FX (source of truth) | OFF | https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html | ✅ with attribution (the reference rates are for information) | Bank FX fixings (WM/Reuters) if a policy requires them |
| FRED H.10 (`fred`) | FX: TWD, pre-1999 | OFF | https://fred.stlouisfed.org/legal/ | ✅ (H.10 is Federal Reserve public data) | n/a |
| Central-bank FX (BCRA, BCRP, Colombia TRM) (`central_bank`) | LATAM FX | OFF | Publisher sites | ✅ with attribution | n/a |
| mindicador.cl | CLP FX (a relay of BCCh data) | UNOFF (third-party) | https://mindicador.cl | ⛔ Use the official source | BCCh SieteRestWS (keys `BCCH_USER` / `BCCH_PASS`) |

### 2.2 Fundamentals and filings

| Source | Used for | Type | Terms | Bank use | Replacement / note |
|---|---|---|---|---|---|
| SEC EDGAR (`sec_edgar`, `sec_fsds`): XBRL companyfacts, submissions, filing text, Forms 3/4/5, 13F, 13D/G, fails-to-deliver | US fundamentals (point-in-time), filings, insiders, ownership | OFF | https://www.sec.gov/os/accessing-edgar-data and https://www.sec.gov/privacy (public domain; fair access ≤ 10 req/s with a declared User-Agent) | ✅ | Keep. It is the authoritative point-in-time source. |
| filings.xbrl.org (`esef_xbrl_org`) | EU/UK ESEF xBRL-JSON | NPO | https://filings.xbrl.org (free; the reports are public regulatory filings) | ✅ with attribution | Keep, or use national storage mechanisms |
| National storage mechanisms: DILA / AMF info-financière (FR), AFM (NL), FSMA STORI (BE), CNMV (ES), eMarket Storage / 1Info (IT), Nasdaq Nordic, Oslo NewsWeb | ESEF annual reports per country | OFF / COMM-run OAM / EXCH | FR: Licence Ouverte 2.0 (https://www.etalab.gouv.fr/licence-ouverte-open-licence). Others: publisher website terms. | FR ✅. NL / BE / ES ✅ with attribution (public registers). IT, Nordic and Oslo ⚠️: website terms; the Nordic and Oslo APIs are undocumented. | Vendor fundamentals, or keep after review |
| FCA National Storage Mechanism (`fca_nsm`) | UK annual reports, director dealings | OFF | https://www.fca.org.uk/legal (undocumented API behind the NSM site) | ⚠️ Public data, but the endpoint is undocumented | Keep after review / vendor |
| Companies House (`companies_house`); key | UK accounts, SIC codes | OFF | https://www.gov.uk/government/organisations/companies-house (Open Government Licence v3.0) | ✅ with attribution | Keep |
| EDINET (FSA Japan) (`edinet`); key | JP annual / half-year XBRL, large holdings | OFF | https://disclosure2dl.edinet-fsa.go.jp (Public Data License 1.0 / CC BY compatible) | ✅ with attribution | Keep |
| TDnet (TSE) (`tdnet`) | JP earnings releases (tanshin) | EXCH | https://www.release.tdnet.info (TSE viewing service, copyright TSE) | ⚠️ Personal-use viewing service | JPX TDnet licensed feed or vendor |
| JPX / KRX KIND lists (`jpx`, kind) | Listed lists, sectors, earnings schedule | EXCH | Exchange website terms | ⚠️ Reference data, low volume | Vendor reference data |
| OpenDART / DART (FSS Korea) (`opendart`, `dart_bulk`); key | K-IFRS statements, disclosures, insider holdings | OFF | https://opendart.fss.or.kr (public data, free use with attribution) | ✅ with attribution | Keep |
| CVM (Brazil) (`cvm`) | DFP/ITR statements, insiders | OFF | https://dados.cvm.gov.br (open data; ODbL on the portal) | ✅ with attribution | Keep |
| BIVA (MX), CMF (CL), SMV (PE) | LATAM statements | EXCH / OFF | Publisher sites | ✅ / ⚠️ BIVA website terms | Keep after review |
| HKEX / HKEXnews (`hkex`) | HK announcements, disclosure of interests | EXCH | https://www.hkexnews.hk (website terms) | ⚠️ Personal, low rate, no redistribution | HKEX data licence / vendor |
| MOPS (TWSE) (`mops`) | TW IFRS iXBRL | EXCH / OFF | Taiwan open data | ✅ with attribution | Keep |
| AKShare (Sina / THS / CNINFO) (`akshare`, `akshare_cn`) | China A-share statements, announcements; some China macro (already excluded from macro computations) | UNOFF | Library MIT; data owned by the sites (research / personal use) | ⛔ | Vendor; CNINFO official for announcements |
| Yahoo fundamentals (`yfinance_fund`) | Snapshot fundamentals fallback | UNOFF | as §2.1 | ⛔ | Vendor fundamentals |
| IFRS / ESEF taxonomies (downloaded by Arelle) | XBRL parsing | NPO | IFRS Foundation taxonomy licence (https://www.ifrs.org/issued-standards/ifrs-taxonomy/) | ✅ Use for processing is permitted | n/a |

### 2.3 Insider, ownership, short interest, positioning

| Source | Type | Terms | Bank use | Note |
|---|---|---|---|---|
| SEC Forms 3/4/5, 13F, 13D/G, FTD | OFF | as SEC above | ✅ | |
| FINRA short interest (`finra`, cdn.finra.org) | OFF (self-regulatory organisation) | https://www.finra.org/terms-of-use | ⚠️ Website terms limit commercial reuse of downloaded data. FINRA offers data licences. | Review, or use the vendor's short-interest data |
| CFTC Commitments of Traders (`cftc`, publicreporting.cftc.gov) | OFF | https://www.cftc.gov/MarketReports/CommitmentsofTraders (US public data) | ✅ | |
| EU/UK net short registers: FCA, AMF, CNMV, AFM, CBI, FSMA, FI, Finanstilsynet (`eu_short`) | OFF | Each regulator's public register (robots.txt respected; FSMA 30 s crawl delay) | ✅ with attribution | |
| BaFin, AFM, AMF director dealings (`bafin`, `afm`, `amf`) | OFF | Public registers (some undocumented exports) | ✅ / ⚠️ | |
| OpenFIGI (`openfigi`) | COMM (free service run by Bloomberg) | https://www.openfigi.com/docs/terms-of-service (FIGI identifiers are free / open) | ✅ | Keep (identifier mapping) |

### 2.4 Macro and central banks

| Source | Type | Terms | Bank use |
|---|---|---|---|
| FRED / ALFRED (`fred`); key `FRED_APIKEY` | OFF | https://fred.stlouisfed.org/legal/ and https://fred.stlouisfed.org/docs/api/terms_of_use.html | ✅ for public-domain series (BLS, BEA, Fed Board, Treasury). **⛔ for third-party copyrighted series**: ICE BofA OAS indices, S&P 500 / DJIA levels, and any series FRED marks "Copyright, ... used with permission". The app keeps an append-only local **ICE BofA OAS archive** (`data/raw/fred/oas_archive`, `app.ingestion.fred`). **Delete or license it (ICE Data Services) before bank use.** |
| ECB Data Portal (`ecb`) | OFF | https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html | ✅ Free reuse with attribution |
| Eurostat (`eurostat`) | OFF | https://ec.europa.eu/eurostat/about-us/policies/copyright (CC BY 4.0) | ✅ |
| Bundesbank, SNB, INSEE, Banco de Portugal BPstat, INE Spain, Destatis, ISTAT | OFF | Publisher terms (open data with attribution; INSEE: Licence Ouverte) | ✅ |
| OECD SDMX (`oecd`, ICIO tables) | OFF | https://www.oecd.org/en/about/terms-conditions.html (CC BY 4.0 since 2024) | ✅ |
| IMF SDMX (`imf`) | OFF | https://www.imf.org/en/About/copyright-and-terms | ✅ with attribution (free data; check the commercial-use clause) |
| World Bank, Pink Sheet (`worldbank`, `wb_pinksheet`) | OFF | https://www.worldbank.org/en/about/legal/terms-of-use-for-datasets (CC BY 4.0) | ✅ |
| BIS (`bis`) | OFF | https://www.bis.org/terms_conditions.htm | ✅ with attribution |
| US Treasury, NY Fed (ACM term premium, r*, staff nowcast), Atlanta Fed GDPNow | OFF | Public | ✅ with attribution |
| StatCan, Bank of Canada, BCB, IBGE, BCRP, BCRA / INDEC, BanRep, INE Chile, BCCh (keyed), INEGI (keyed), Banxico (keyed), BoE, ONS (Open Government Licence), BoJ, MoF Japan, e-Stat (keyed), BoK ECOS (keyed), HKMA, C&SD, RBA, ABS (CC BY), CBC Taiwan, Riksbank, Norges Bank, NIFC / ChinaMoney LPR | OFF | Publisher terms: open data, generally with attribution | ✅ |
| Release calendars (BLS / BEA ICS, FOMC, Eurostat, ECB, ONS, BoE, BoJ, StatCan, BoC, ABS, RBA, IBGE, BCB, INSEE, Destatis, ISTAT via a public Google Calendar ICS, INE) | OFF | Publisher sites | ✅ |
| UN Comtrade (`comtrade`); optional key | OFF (UN) | https://comtradeplus.un.org (terms of use) | ⚠️ The free tier is for non-commercial use. Commercial use needs a subscription. |
| Caldara–Iacoviello GPR index | NPO / academic | https://www.matteoiacoviello.com/gpr.htm | ⚠️ Academic data; cite. Confirm before commercial use. |

### 2.5 News, events and text

| Source | Type | Terms | Bank use | Replacement |
|---|---|---|---|---|
| GDELT 2.0 files and DOC API (`gdelt_*`) | NPO | https://www.gdeltproject.org/about.html#termsofuse ("open for unlimited and unrestricted use for any academic, commercial, or governmental use … with citation") | ✅ with citation | n/a |
| Common Crawl CC-NEWS WARC files (`ccnews_articles`, story source `ccnews`; app.events_v2.ccnews) | NPO (crawl) / publisher copyright (content) | Common Crawl Terms of Use, https://commoncrawl.org/terms-of-use (last updated 2024-03-07): limited, non-exclusive licence to access and use the Service; the Crawled Content "may be subject to separate terms of use or terms of service from the owners of such Crawled Content"; no use that violates intellectual-property rights, no harvesting of personal information, no disruption of the servers. **How we use it:** derived metadata only (headline, <= 300-character meta description, URL + hash, publisher domain, language, publication time, issuer / country / sector / theme labels, story sentiment, MinHash band keys). Article bodies are never decompressed into storage (the parser stops at the HTML head), WARC files are streamed in memory and never kept on disk. Etiquette: <= 6 concurrent connections, identifying User-Agent, exponential back-off on 429 / 503 "Slow Down", Range-resume instead of re-downloads. | ⚠️ Publisher copyright on headlines / descriptions: fine for personal research; a bank needs legal review or licensed news (Dow Jones / Factiva, LSEG, Bloomberg) | Licensed news archive |
| Official RSS feeds: Fed, ECB, SEC, BoE, BoJ, BLS, BEA, Eurostat, ONS | OFF | Publisher terms | ✅ | |
| Polygon / Massive news (`polygon_news`) | COMM | as §2.1 | ⚠️ plan-dependent | Licensed news: Dow Jones / Factiva, LSEG News, Bloomberg |
| Google News RSS (`rss_google_news`) | UNOFF | Google ToS discourage automated access | ⛔ (already off by default) | Licensed news |
| Loughran–McDonald dictionary (lazy prices tone) | NPO / academic | https://sraf.nd.edu/loughranmcdonald-master-dictionary/ (free for academic use; commercial licence available) | ⚠️ The app ships only built-in word subsets; the full file is user-supplied | Buy the commercial licence or keep the subsets |

### 2.6 Reference, identity and geography

| Source | Type | Terms | Bank use |
|---|---|---|---|
| GLEIF LEI data (`gleif`) | NPO (regulatory) | https://www.gleif.org/en/meta/lei-data-terms-of-use (CC0) | ✅ |
| ESMA FIRDS (`esma_firds`) | OFF | https://www.esma.europa.eu/legal-notice (reuse with attribution) | ✅ |
| SEC company tickers, EDINET / OpenDART code lists, CVM register | OFF | as above | ✅ |
| SSE / SZSE company lists | EXCH | Website terms | ⚠️ |
| Natural Earth (`natural_earth`, plus world-atlas in the UI) | NPO | https://www.naturalearthdata.com/about/terms-of-use/ (public domain) | ✅ |
| UN Comtrade, IMF IMTS, OECD ICIO, BIS LBS/CBS (linkage) | OFF | as §2.4 | ✅ / ⚠️ Comtrade |

### 2.7 ML model weights (downloaded from huggingface.co on first use)

| Model | Use | Licence | Bank use |
|---|---|---|---|
| `ProsusAI/finbert` | English financial sentiment | Not stated on the model card at the time of review. The FinBERT paper and code are by Prosus. | ⚠️ Confirm with the publisher, or replace with an Apache / MIT licensed model |
| `cardiffnlp/twitter-xlm-roberta-base-sentiment` | Multilingual sentiment | Check the model card (Cardiff NLP; base model XLM-R is MIT) | ⚠️ Confirm |
| `mrince/CBRT-RoBERTa-HawkishDovish-Classifier` | Central-bank tone | MIT (recorded in `app/macro/cb/tone.py`) | ✅ |
| Rejected in the repo, not used: gtfintechlab FOMC-RoBERTa (CC-BY-NC), tabularisai multilingual (CC-BY-NC-4.0) | | | n/a |

In a bank deployment, mirror the approved model weights on an internal artefact store and set `HF_HUB_OFFLINE=1`, so that nothing is fetched at runtime.

### 2.8 Rates and debt (W3, 2026-10-31; owner rates-w3)

Every source below was checked on 2026-10-31 (robots.txt and the published terms). Requests are at most one per second per host, with a descriptive User-Agent and an on-disk cache; large archives are downloaded once into `data/raw/rates/`.

| Source (source_id) | Used for | Type | Terms | Bank use |
|---|---|---|---|---|
| US Treasury daily par / real yield curve XML (`ust`), home.treasury.gov | `yield_curves` US_TSY, US_TSY_REAL | OFF | US government work, public domain; robots.txt `Allow: /` | ✅ |
| US Treasury Fiscal Data API (`fiscaldata`), api.fiscaldata.treasury.gov | `sovereign_auctions` (auction results since 1979, upcoming auctions) | OFF | Public domain; fiscaldata robots.txt `Allow: /` | ✅ |
| Federal Reserve H.15 via FRED (`fred`: DGS*, DFII*) | US_H15, US_H15_TIPS | OFF | as FRED (H.15 is Federal Reserve public data) | ✅ |
| ECB Data Portal (`ecb`): YC Svensson parameters, IRS convergence yields, TGB TARGET balances, EUR STR | EA_AAA, EA_ALL, {EU}_LT10, TARGET balances | OFF | ECB statistics reuse policy (free reuse with attribution) | ✅ |
| Deutsche Bundesbank time-series API (`bundesbank`): BBSIS Svensson parameters | DE_BUND (full curve reconstructed) | OFF | Bundesbank terms (free reuse with source) | ✅ |
| Bank of England yield-curve archive (`boe`): media zips glcnominal / glcreal / glcinflation / ois + latest | GB_GILT, GB_GILT_REAL, GB_INFLATION, GB_OIS | OFF | BoE copyright, reuse with attribution; the zips are under /-/media (not disallowed by robots.txt) | ✅ with attribution |
| Ministry of Finance Japan JGB interest rates CSV (`mof_jp`) | JP_JGB | OFF | MoF website terms (public data, attribution) | ✅ |
| Banco de España BIE CSV tables TI_1_3 / TI_1_7 (`bde`) | ES_BONO; EA_EURIBOR | OFF | BdE reuse terms (attribution). **EURIBOR is EMMI's benchmark** (BdE republishes it from Refinitiv with the EMMI terms note) | ✅ ES yields; ⚠️ EURIBOR: personal use, a bank needs an EMMI licence |
| Banco de Portugal BPstat API (`bpstat`): OT yields 2-10y (IGCP benchmarks) | PT_OT | OFF | BPstat terms (attribution) | ✅ |
| SNB data portal cube rendeiduebd (`snb`) | CH_CONF | OFF | SNB terms (free use with source) | ✅ |
| Bank of Canada Valet (`boc`) | CA_GOC | OFF | https://www.bankofcanada.ca/terms/ (attribution) | ✅ |
| RBA statistical tables F2 / F17 CSV (`rba`) | AU_ACGB | OFF | RBA copyright, CC BY 4.0 for statistical tables; robots disallow /assets only | ✅ |
| Riksbank SWEA API, Norges Bank SDMX (`riksbank`, `norges`) | SE_SGB, NO_NGB | OFF | Open data with attribution | ✅ |
| OECD KEI / MEI long-term rates (lake series `oecd:KEI.*.IRLT`, `fred:IRLTLT01*`) | {CC}_LT10 for non-EU countries | OFF | as §2.4 (CC BY 4.0) | ✅ |
| NY Fed Markets Data API (`nyfed_rates`): SOFR, EFFR, OBFR, TGCR, BGCR, SOFR averages / index | macro_observations (money markets) | OFF | NY Fed terms of use (public reference rates; attribution) | ✅ |
| Eurostat gov_10q_ggdebt / gov_10q_ggnfa (all EU27), IMF WEO (existing) | Sovereign debt / GDP, deficits | OFF | as §2.4 | ✅ |
| BIS WS_CBPOL (21 more central banks) | Policy rates | OFF | as §2.4 | ✅ |
| FRED ICE BofA OAS / effective yields by rating, maturity, EM sector and region (`fred:BAML*`) | Credit page, refinancing proxy | COMM via OFF | ICE Data copyright, FRED personal use (see §2.4) | ⛔ without an ICE licence (labelled personal use) |
| FRED Moody's seasoned Aaa / Baa yields (`fred:DAAA`, `fred:DBAA`) | Credit page | COMM via OFF | Moody's copyright, FRED personal use | ⛔ without a Moody's licence (labelled) |
| UK DMO data reports (`dmo`), dmo.gov.uk XmlDataReport D2.1E | Gilt issuance history | OFF | Crown copyright / OGL; no robots.txt. The site runs a bot-protection challenge after a few requests: one request per run, a challenge stops the source (never bypassed) | ✅ |
| ESMA FIRDS FULINS_D (`esma_firds`) | `bond_reference` EU / EEA listed bonds (joined by LEI) | OFF | as §2.6 (reuse with attribution) | ✅ |
| OpenFIGI API (`openfigi`), /v3/search without key (5 / min) | `bond_reference` US TRACE bonds (FIGI, coupon / maturity parsed from the description) | COMM (free) | https://www.openfigi.com/docs/terms-of-service (FIGIs are open data); www.openfigi.com robots disallows /search (the web UI), the API host has no robots.txt | ✅ (no ISIN / CUSIP stored) |
| FINRA API (`finra_api`), keyed (FINRA_API_CLIENT_ID / SECRET) | `credit_market_stats` TRACE aggregates | OFF (SRO) | FINRA API terms (free account for public datasets) | ⚠️ review FINRA API terms; off until keys are set |
| Yahoo delayed quotes (^IRX, ^FVX, ^TNX, ^TYX) through the existing quotes service | Curve monitor live strip | UNOFF | as §2.1 | ⛔ (personal use; labelled) |

**Rejected (not used)**

| Source | Reason |
|---|---|
| TreasuryDirect TA_WS API | treasurydirect.gov robots.txt: `User-agent: * Disallow: /` (only usasearch allowed) → Fiscal Data API instead |
| Fed H.15 Data Download Program | Being retired (week of 2026-11-09) → H.15 via FRED |
| Banque de France Webstat (TEC rates) | Keyless Opendatasoft API serves the catalogue only (`has_records: false`); observations need the keyed BdF API → France from ECB IRS (monthly) |
| Banca d'Italia Infostat | Answers "Risorsa Protetta"; daily BTP benchmarks are MTS (commercial) → Italy from ECB IRS (monthly) |
| EMMI EURIBOR files | robots.txt disallows /media and /assets (the download paths) → EURIBOR as republished by Banco de España |
| BoE IADB database | robots.txt disallows `/boeapps/database/_iadb-FromShowColumns.asp` → W3 uses only the yield-curve archive. **Note:** the existing macro fetcher `boe` (app.ingestion.macro_national, SONIA / Bank Rate / gilt yields) calls `_iadb-fromshowcolumns.asp` (lower-case variant of the disallowed path); review it (owner macro-coverage). **Resolved 2026-11-07 (COVERAGE_V3 A9):** the fetcher no longer calls the IADB; see docs/it/ROBOTS_COMPLIANCE.md |
| FINRA Fixed Income Data website (bond search, market activity widgets) | Terms of use: "You are not permitted to … use any robot, spider, other automatic or manual process to monitor or copy the Data" → not used; US bonds via OpenFIGI, TRACE aggregates only through the keyed FINRA API |
| AFT (France debt agency) | Cloudflare bot challenge (HTTP 403 "Just a moment") |
| Finanzagentur issuance calendar / results | PDF only (no machine-readable feed) |
| MEF (IT), Tesoro (ES), IGCP (PT) auction calendars / results | PDF / HTML press releases only (IGCP debt-stock xlsx noted for a later iteration) |

### 2.9 Software libraries (third-party Python packages added after the SBOM baseline)

The full component list with licences is in `docs/it/sbom.json` (regenerate with `scripts/make_sbom.py`).

| Package (pinned) | Use | Licence | Bank use |
|---|---|---|---|
| `lightgbm` 4.7.0 | Model Studio: LightGBM regression / classification, LambdaRank, DML nuisance learner (Studio v2 S2) | MIT | ✅ |
| `optuna` 5.0.0 (+ SQLAlchemy 2.1.8, alembic 1.20.1, Mako 1.4.3, colorlog 6.12.0) | Model Studio: hyper-parameter search on purged walk-forward CV; in-memory storage only, no database is created | MIT (SQLAlchemy, alembic, Mako, colorlog: MIT) | ✅ |
| `ngboost` 0.5.11 (+ lifelines 0.30.3, autograd 1.9.1, autograd-gamma 0.5.0, matplotlib 3.11.2) | Model Studio: NGBoost probabilistic regression (Normal location / scale), Studio v2 S3; lifelines / matplotlib are import-time dependencies only (no plots are written) | Apache-2.0 (lifelines MIT, autograd MIT, autograd-gamma MIT, matplotlib PSF-based Matplotlib licence) | ✅ |
| `torch` (already present) | Model Studio neural networks (MLP, GRU / LSTM, autoencoder), CPU only | BSD-3-Clause | ✅ |

## 3. Attribution notice

The UI's System page credits GDELT, Natural Earth and TradingView Lightweight Charts. A bank deployment should add an "Data sources" page listing the attributions for SEC, ECB, Eurostat (CC BY 4.0), OECD (CC BY 4.0), World Bank (CC BY 4.0), the IMF, BIS, ONS / Companies House (OGL v3), EDINET, OpenDART, CVM, GDELT and Natural Earth. The data behind it is the "Terms" column above.

## 4. Vendor adapter interface (licensed price / fundamentals feed)

`backend/app/ingestion/vendors.py` defines how a licensed feed plugs in. Tests are in `backend/tests/ingestion/test_o4_vendors.py`.

```python
@runtime_checkable
class MarketDataVendor(Protocol):
    vendor_id: str
    licence: VendorLicence            # vendor, terms_url, commercial_use, internal/external redistribution, attribution

    def capabilities(self) -> frozenset[str]: ...                       # eod_prices, corporate_actions, fundamentals, reference, fx
    def resolve(self, refs: Sequence[InstrumentRef]) -> pl.DataFrame: ... # ISIN / FIGI / ticker+MIC -> vendor symbol
    def eod_prices(self, symbols, start: date, end: date) -> pl.DataFrame: ...          # -> prices_staging columns
    def corporate_actions(self, symbols, start: date, end: date) -> pl.DataFrame: ...   # -> corporate_actions_staging
    def fundamentals(self, entities, since: date | None = None) -> pl.DataFrame: ...    # -> fundamentals_raw (point-in-time)
```

**How it fits:**
- A vendor writes the **same staging tables** as today's adapters. `ingest_prices()`, `ingest_corporate_actions()` and `ingest_fundamentals()` fill the bookkeeping columns, validate against the table contract and write.
- Everything downstream is unchanged and source-agnostic: `prices_finalize` (identity-break, unit-flip and unfinished-bar guards; official FX conversion), corporate actions, valuation, scoring, ML and serving.
- **Point-in-time rule:** `fundamentals_raw.knowledge_date` must be the date the figure became known. This is the vendor's "as first reported" or point-in-time history, never the period end. Otherwise backtests and models leak future data.
- **Contract rule:** `prices_staging.source_id` is an enum. A new vendor writes `other` until its id is added to the enum. That is an additive change, logged in `docs/CONTRACT_CHANGES.md`, together with its precedence in `app.normalize.prices`.

**Implementations:**

| Class | Status | Feed |
|---|---|---|
| `FileDropVendor` | **Working** | Vendor end-of-day files (CSV / Parquet) dropped in `data/vendor_drop/<vendor>/{prices,corporate_actions,fundamentals}/`, with a column map. This is how Bloomberg Data License, LSEG DataScope Select and FactSet bulk deliveries usually arrive (SFTP). Example map: `{"TICKER":"symbol","DATE":"date","PX_LAST":"close","PX_VOLUME":"volume","CRNCY":"currency"}`. |
| `BloombergVendor` | Skeleton | `blpapi` (Server API / B-PIPE) HistoricalDataRequest (PX_OPEN/HIGH/LOW/LAST/VOLUME, DVD_HIST_ALL), or Data License files through `FileDropVendor` |
| `LsegVendor` | Skeleton | LSEG Data Library for Python (`lseg-data`), Refinitiv Data Platform REST, or DataScope Select |
| `FactSetVendor` | Skeleton | FactSet Prices / Fundamentals / Formula APIs (OAuth client credentials) |
| `InternalServiceVendor` | Skeleton | The bank's market-data service (golden source): REST or gRPC behind the gateway with an Entra ID client-credentials token |

**Selection:**
- `BIG360_PRICE_VENDOR` takes `none` (default), `file_drop`, `bloomberg`, `lseg`, `factset` or `internal`, and `get_vendor()` returns the adapter.
- Wiring it into the DAG is a one-job change in `contracts/jobs.yaml`: a `prices_vendor` job before `prices_finalize`. In the same change, the `prices_global_yf`, `yf_fundamentals`, `futures_curve`, `cn_akshare` and `baostock_*` jobs are disabled in the bank profile.
- Credentials come from Credential Manager or Key Vault through Settings, never from code.

**Effort per vendor:** about 2–5 days, assuming an entitlement and SDK access. The work is the symbology map in `resolve()`, field mapping, pagination and throttling, and a golden check against today's data (`app.qa.golden`).
