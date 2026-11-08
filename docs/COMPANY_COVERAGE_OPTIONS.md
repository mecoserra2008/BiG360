# BiG Equity 360: Company Fundamentals Coverage Options (non-US)

*Research date: 2026-10-11.*
- **Read-only research.** Nothing in the project was changed except this file.
- **Live-tested** means a real HTTP call was made from this machine (Portugal) and a real sample was parsed.
- Scratch scripts and samples are in `%TEMP%\claude\...\scratchpad\coverage_research\{asia,europe,latam_other,commercial}`.
- This doc complements `DATA_SOURCES_AMERICAS_EUROPE.md` and `DATA_SOURCES_ASIA_MACRO_EVENTS.md`. It does not repeat sources already used there except where we found a pipeline problem with them.

Legend:
- **Status:** ✓ = live-tested OK · D = docs-only (key or subscription needed, endpoint answered 401/403 as expected) · ✗ = blocked or dead.
- **Effort:** S ≤ 1 day adapter · M = 2–5 days · L = more than a week or high maintenance.
- **PIT** = point in time: is there a publication/filing timestamp we can use as `knowledge_date`?

---

## 0. TL;DR

1. **A large part of the non-US gap is our own pipeline, not missing sources** (43 of the 112 gap names have official data we already have access to, and yfinance covers all 112). We measured the MVP universe: 757 = 509 US + 248 global. 112 of the 248 global names lack a latest-FY statement.
   - **`yfinance_fund` has only been run for 20 issuers.** It returns the latest FY for every gap name we tried: BMW, Nestle, 0388, 3690, Hyundai, Reliance, Hon Hai, CBA, DBS, Grupo Mexico, Fast Retailing. That is 4–5 annual periods without PIT.
   - **filings.xbrl.org already holds FY2025 ESEF for 29 gap names** (DSV, Maersk, Kone, Nordea, AXA, Vinci, Dassault, Engie, Thales, Pernod, Hermès, Safran, Schneider, Generali, Ahold, Adyen, Airbus, ASM, Wolters Kluwer, Tesco, Diageo, LSEG, Rolls-Royce, BAE, Anglo, Compass, StanChart, Experian, Glencore). The ESEF job most likely ran the recent-window path (`max_filings=400`) instead of the per-LEI path. The source is complete; our run was not. Adding three missing LEIs (BNP, Heineken, 3i) makes it **32**.
   - **SEC `companyfacts` silently stops at FY2024 for IFRS 20-F filers.** The 2025 IFRS taxonomy facts are not in companyfacts or frames. The quarterly **Financial Statement Data Sets (FSDS)** do contain them. Verified for TSMC: ProfitLoss FY2025 = NT$1,697.6bn, filed 2026-04-16. FSDS fills **TSMC, KB, Shinhan, POSCO, SQM, Ecopetrol, Cemex, Credicorp, Telefónica, Diageo** with history from 2009 and the `filed` date.
   - **EDINET backfill only covers recent dates.** `since = today-7d`, so only about 2 years exist even for Toyota. That is why Fast Retailing and Seven & i are missing.
2. **Free official sources exist for almost everything outside DE, CH, PT, AU and SG:**
   - **Korea:** DART bulk zips, no key.
   - **Hong Kong:** Eastmoney HK F10 via AKShare, all 10 HK gaps with 10–26 years.
   - **Taiwan:** MOPS bulk IFRS XBRL from 2013 plus upload timestamps.
   - **India:** NSE results JSON and XBRL, which work without cookies.
   - **France:** DILA info-financière OpenDataSoft API, with all 16 FR names.
   - **Netherlands:** AFM register export.
   - **Italy:** eMarket Storage and 1Info.
   - **Nordics:** Nasdaq Nordic news API and Oslo NewsWeb.
   - **Belgium:** FSMA STORI JSON.
   - **Spain:** CNMV ESEF plus the semiannual IPP XBRL, which goes back to 2008.
   - **Japan Q1/Q3:** TDnet tanshin XBRL, scraped daily.
   - **Mexico:** BIVA JSON plus `ifrs-full` XBRL, quarterly from 2018.
   - **Chile:** CMF per-issuer XBRL from 2009.
   - **Peru:** SMV, with filing timestamps.
   - **Germany:** no free route until ESAP (public access 10 Jul 2027). **Switzerland:** none (no XBRL mandate). Both stay on yfinance, a paid vendor or PDF extraction.
3. **Resulting coverage (latest FY from a non-yfinance, PIT-dated source):**
   - Free tier: from **121/248 to about 218/248** global names. 121 is the current non-yfinance count; 136 includes yfinance.
   - With yfinance as the no-PIT fallback: **248/248**.
   - Tier details are in §9.
4. **About €50/month:** EODHD "Fundamentals" (€49.99/month billed annually) is the only plan at this price with standardized global statements and a per-period `filing_date`. **However, its licence is Non-Professional only.** An employee using it for BiG is "Professional", and the internal-use licence is €399/month. FMP Ultimate ($99–139) has the same licence problem.
5. **Institutional:** if BiG holds **FactSet** or **S&P Capital IQ API** entitlements, those are the only server-friendly, PIT-correct and licensed routes. FactSet has a `/point-in-time` endpoint; `spgmiciq` has `get_income_statement_pit`. Bloomberg Desktop API and LSEG Workspace work only on a desktop with a logged-in terminal, and their licences prohibit a persisted shared database. **The IBKR TWS API removed `reqFundamentalData` in v10.47 (2026): dead.**

---

## 1. Baseline: what is actually missing (measured)

Method: from `normalized/fundamentals_canonical`, count an issuer as covered if it has an FY `revenue` or `net_income` fact with `period_end ≥ 2025-03-01` from any source. "Official" excludes `yfinance`. Issuer-level counts for `mvp_global` (248). The user's figures are listing-level and slightly different; the picture is the same.

| Region | Names | Latest FY (any) | Latest FY (non-yfinance) | Gap names (no latest FY at all) |
|---|---|---|---|---|
| EU | 91 | 28 | 22 | 63: **DE 14**, FR 16, **CH 9**, NL 7, IT 4, SE 4, ES 3, DK 2, FI 2, NO 1, BE 1 |
| UK | 27 | 16 | 16 | 11: TSCO DGE III RR LSEG BA AAL CPG STAN EXPN GLEN |
| HK | 24 | 14 | 10 | 10: 2020 3690 3988 0883 1810 0388 0016 0669 0002 0001 |
| KR | 15 | 2 | 0 | 13 (all but 005930 and 000660, which are yfinance-only) |
| JP | 40 | 38 | 38 | 2: 3382, 9983 |
| CN | 15 | 15 | 15 | 0 (H-share-only names are in HK) |
| LATAM | 22 | 16 | 13 | 6: SQM, EC, GMEXICO, GFNORTE, CEMEX, BAP |
| APAC | 10 | 3 | 3 | 7: CSL, CBA, RELIANCE, TCS, D05, 2330, 2317 |
| CA | 4 | 4 | 4 | 0 |
| **Total** | **248** | **136** | **121** | **112** |

US: 488 of 509 per the user's count. It is not in scope here; SEC covers the US.

History depth is a second problem.
- **EDINET** has only FY2025–26, even for Toyota.
- **ESEF** starts at FY2020/21 by construction.
- yfinance has 4–5 years.

10-year history therefore needs FSDS, DART bulk, MOPS, CNMV IPP, NSE, Eastmoney HK, EDINET backfill, or a paid vendor.

---

## 2. Pipeline fixes first (free, S effort, biggest gain)

| # | Fix | Verified evidence | Names filled (latest FY) | History / PIT |
|---|---|---|---|---|
| F1 | **Run ESEF per LEI for every EU/UK MVP issuer.** `esef.py` already has `fetch(entities=[LEI…])` and `/api/entities/{LEI}/filings`. Run it for all MVP LEIs instead of the recent-window plus `max_filings=400` path. | ✓ Swept 68 gap LEIs: 48 have filings and 29 have FY ending on or after 2025-03, all with `json_url` | **29**: DSV, MAERSK-B, KNEBV, NDA-FI, CS, DG, DSY, ENGI, HO, RI, RMS, SAF, SU, G, AD, ADYEN, AIR, ASM, WKL, TSCO, DGE, LSEG, RR, BA, AAL, CPG, STAN, EXPN, GLEN. FY≤2024 history for 16 more (UCB, AMS, CABK, AI, BN, KER, MC, ENEL, ISP, UCG, PRX, DNB, INVE, HM, ATCO, VOLV) | FY2020→. `date_added` is conservative |
| F2 | **Fill missing LEIs via GLEIF** (already integrated). Missing: RO/ROG, ALV, ENR, SIE, TEF, BNP, EL, ORA, HEIA, VOLV, III. **Do not trust a name-only GLEIF match.** "Orange" returned the wrong entity, and AXA's ESEF filer LEI `9695007ZQTEMPGIL5N67` differs from ours, so match on ISIN. | ✓ BNP `R0MUWSFPU8MPRO8K5P83` (FY2025 on xbrl.org), Heineken N.V. `724500K5PTPSST86UQ23` (FY2025), 3i `35GDVHRBMFE7NWATNM84` (FY 2026-03), Volvo `549300HGV012CNC8JD22` (FY2024) | **+3** (BNP, HEIA, III) | as F1 |
| F3 | **SEC FSDS** (`https://www.sec.gov/files/dera/data/financial-statement-data-sets/{YYYY}q{N}.zip`, about 60 MB per quarter, `sub.txt`/`num.txt`) for IFRS 20-F filers, because `companyfacts` has no ifrs/2025 facts. | ✓ `companyfacts` latest FY = 2024 for TSM, KB, SHG, PKX, SQM, EC, CX, BAP; `frames/ifrs-full/Revenue/*/CY2024` returns 404. FSDS 2026q2 has all 8 20-F filings (`afs=1-LAF`, `filed` 2026-04-16…30) with `version=ifrs/2025`, e.g. TSM ProfitLoss 20251231 = 1,697,604,000,000 TWD and Assets = 7,932,842,500,000. Filter `segments` empty and `coreg` empty (dimensional duplicates exist). | **9 net** (2330.TW, 105560, 055550, 005490, SQM-B, ECOPETROL, CEMEXCPO, BAP, TEF.MC (CIK 814052, missing from our identifier map)) plus Diageo FY2026 (overlaps F1) | 2009→, `filed` = PIT. Also future-proofs every IFRS FPI (Novartis, UBS, Sony, etc.) as companyfacts lags taxonomies |
| F4 | **EDINET backfill over all calendar days** (EDINET documents are kept for about 10 years; `edinet.py` defaults to `since=today-7d`). | ✓ (Asia agent) 9983 and 3382 both file 有価証券報告書. Toyota has only FY2025–26 from EDINET | **2** (9983, 3382). Plus **10 years of history for all 40 JP names** | `submitDateTime` = PIT |
| F5 | **Run `yfinance_fund` for all non-US MVP names** as the no-PIT fallback. Today it runs for 20 issuers only. | ✓ 11 gap names all returned the latest FY (BMW FY2025 revenue €133.45bn; CBA FY 2026-06; Reliance FY 2026-03) | **all 112** latest FY, no PIT, 4–5 years. The only source for DE/CH/AU/SG/MX until something better | `knowledge_date_estimated=True` (period end + 90 days) |

After F1–F4: **43 more names have an official, PIT-dated latest FY**, from 121 to 164. After F5, all 248 have *some* latest FY.

---

## 3. Europe (per country)

Common parser: **Arelle** (`arelle-release` 2.46.1, installs fine on Py 3.13) using `python -m arelle.CntlrCmdLine --plugins saveLoadableOIM -f <package.zip|.xbri> --saveLoadableOIM out.json`. It emits the same xBRL-JSON our `app/ingestion/_xbrl_json.py` already parses. Runtimes and checks:
- Heineken: 5.5 s, 568 facts. Enel: 10 s, 1,065 facts.
- The first run takes 39 s to cache the IFRS/ESEF taxonomies (26 MB).
- Verified values:
  - Danone FY2025: revenue €27,283m, ProfitLoss €1,887m.
  - Heineken FY2025: revenue €34,257m.
  - Enel FY2025: revenue €78,057m.

Pitfalls:
- Load the **zip/.xbri package**, not the inner xhtml, or concepts resolve to None.
- Use OIM JSON, not `--facts` CSV, which uses locale formatting.
- OIM period end is exclusive.
- Dedupe facts that repeat across statements.
- `.xbri` (Report Package 1.0) works.

| Country | Option (ranked) | Status | Endpoint / how | History | PIT | Effort | Fills | Legal |
|---|---|---|---|---|---|---|---|---|
| **FR** | 1. **DILA/AMF info-financière OpenDataSoft API** | ✓ | `GET https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records?where=identificationsociete_iso_cd_lei="<LEI>" and (fichierdecontenu_inf_fic_nom like "*.zip" or fichierdecontenu_inf_fic_nom like "*.xbri")&order_by=informationdeposee_inf_dat_emt desc`. 538k records, same-day refresh. Fields: `informationdeposee_inf_dat_emt` (UTC timestamp), `identificationsociete_iso_cd_lei`, `..._isi`, `informationdeposee_inf_stp_inf` (300001–7 URD, 002002/003000 AFR, 002003/004000 half-year), `url_de_recuperation`. Mirror: `https://echanges.dila.gouv.fr/OPENDATA/AMF/` | ESEF FY2021/22→ (FY2021 often missing); PDFs older | **Excellent** (deposit timestamp) | S | **All 16 FR gaps**; 6 not on xbrl.org: AI, BN, KER, MC, EL, ORA | Licence Ouverte (open, commercial OK) |
| | 2. filings.xbrl.org (F1) | ✓ | — | FY2020→ | conservative | done | 10 of 16 | — |
| **NL** | 1. **AFM register export** | ✓ | `https://www.afm.nl/export.aspx?type=e8825b05-4004-4301-b736-651e8c61053d&format=xml` (or `csv`), 9,731 rows from 2007. Fields: `datum` (publication timestamp, US format), `uitgevende-instelling`, `boekjaar`, `objecttype_eng` (Annual / Half-yearly). Download: details page `…/financiele-verslaggeving/details?id=A2510-03613`, then the encrypted `downloadregisterfile.aspx?…&enc=…` link | ESEF FY2021→ (PDF from 2007) | Good | S | PRX (+ HEIA, AD, ADYEN, AIR, ASM, WKL redundantly) | public register |
| **IT** | 1. **eMarket Storage** | ✓ | `https://www.emarketstorage.it/en/node/30?categoria=100&azienda=<id>&page=N` (100 = AFR, 101 = half-year). Ids: Enel 1003, Generali 236, Intesa 142, UniCredit 198. Rows `div.views-row` with `data-protocollo`, time, `/sites/default/files/xbrl/YYYY-MM/YYYYMMDD_<prot>.zip` | ESEF FY2021→ | Good (Enel ESEF 2026-04-13 18:11; AFR PDF 2026-03-19) | S–M | ENEL, ISP, UCG (+G) | website terms |
| | 2. **1Info** | ✓ | `GET https://www.1info.it/PORTALE1INFO/API/companies/documenti` (314 issuers); `POST …/API/Documenti` (`SearchFilter[emittente]`, `SearchFilter[oggetto]=ESEF`); fields `dataStoccaggio`, `protocolCodeXbrl`, `bilancio_consolidato` | FY2021→ | Good | S | issuers stored at 1Info (ENI, Terna, Snam, Prysmian, Moncler…) | website terms |
| **ES** | 1. **CNMV annual ESEF (IFA)** | ✓ | `https://www.cnmv.es/Portal/Consultas/IFA/ListadoIFA.aspx?id=0&nif=<NIF>`, which lists the publication date plus `webservices/verdocumento/ver?e=<token>` (consolidated xhtml or `DOC.XBRI` package). The NIF format is inconsistent (`A-84236934` for Amadeus) | ESEF FY2020→ | Good | M | TEF, AMS, CABK (+IBE, ITX) | public |
| | 2. **CNMV semiannual IPP XBRL** (own taxonomy) | ✓ | `…/Consultas/IFI/ListaIFI.aspx?nif=<NIF>` → `detalleifialdia.aspx?nreg=…` → `descargaxbrlipp.ashx?t={guid}`. Concepts `I1205` revenue, `I1288` consolidated result, `I1300` attributable, `I1100` total assets. Banks use `ipp/en`. Telefónica H1-2026 revenue €16,391.7m | **H2-2008→, about 36 half-years**, so 10y+ | Good (publication date on the list) | M | same; **semiannual history** | public. WAF returns 403 `CVFE` on malformed requests |
| **BE** | 1. **FSMA STORI JSON** | ✓ | `POST https://webapi.fsma.be/api/v1/en/stori/result` `{"companyName":"UCB","documentTypeId":"9813c451-…","pageSize":20,…}` gives `datePublication, dateReceived, lei, mainDocuments[].fileDataId`; then `GET …/stori/download?fileDataId=…` | ESEF FY2021→, PDF 2011→ | **Excellent** | S | UCB | public |
| **SE** | 1. **Nasdaq Nordic news API** | ✓ | `GET https://api.news.eu.nasdaq.com/news/query.action?type=json&showAttachments=true&company=<name>&globalGroup=exchangeNotice&globalName=NordicAllMarkets&limit=200`. Fields `published`, `cnsCategory="Annual Financial Report"`, `attachment[].attachmentUrl` (ESEF zip) | about FY2020→ | **Excellent** (release time) | S | ATCO, HM, INVE, VOLV | undocumented API; ToS not reviewed |
| | 2. FI Finanscentralen (OAM) | ✓ | ASP.NET postback search, `GetFile.aspx?fid=<int>` | 2017→ | **None** (no publication date) | M | same | public |
| **DK / FI** | Nasdaq Nordic news API (as SE). Nasdaq Helsinki is the Finnish OAM | ✓ | Kone FY2025 zip 10.5 MB; DSV FY2022–25 | about FY2020→ | Excellent | S | (DSV, MAERSK, KNEBV, NDA-FI already via F1) | as above |
| | DK Finanstilsynet OAM | ✗ | issuer-submission SPA, no public search API | — | — | — | — | — |
| **NO** | **Oslo Børs NewsWeb API** | ✓ | `POST https://api3.oslo.oslobors.no/v1/newsreader/list?issuer=DNB&fromDate=2010-01-01&toDate=…&category=1001` (with Origin/Referer `https://newsweb.oslobors.no`), then `…/message?messageId=` (`publishedTime`, attachments), then `…/attachment?messageId=&attachmentId=` | 2017→ | **Excellent** | S | DNB | undocumented API |
| **PT** | CMVM | ✗ | OutSystems SPA; screen-service POST returns `{"exception":{"message":"No role validation found"}}` (needs a browser session) | — | — | L (headless browser) | (EDP, GALP stuck at FY2024 on xbrl.org) | — |
| **DE** | Unternehmensregister / Bundesanzeiger | ✗ | captcha per publication ("Sicherheitsabfrage"), no API; xbrl.org has 0 DE filings | — | — | — | none | — |
| | IR sites | partial | BMW PDF only (Akamai, needs curl_cffi); Deutsche Telekom publishes **XLSX** statements (`report.telekom.com/annual-report-2025/_assets/downloads/cfs-financial-statements-dtag-ar25.xlsx`); Siemens Energy points to Unternehmensregister | per issuer | weak | L per issuer | DTE (xlsx) | — |
| **CH** | SIX / issuers | D | No XBRL mandate, no structured SIX data. IR PDFs, some Excel | — | — | L | — | — |
| **IE / PL** | Euronext Dublin OAM / GPW ESPI | D / partial | no IE or PL gap names; xbrl.org has 877 PL filings | — | — | — | — | — |
| **EU-wide** | **ESAP** | D | Collection started 10 Jul 2026; **public access 10 Jul 2027**; phase 2 Jan 2028 | — | — | — | DE, PT, all EU (future) | open |
| | IR-site ESEF discovery at scale | ✗ (not scalable) | Only some issuers post ESEF (EDP yes; BMW, DTE, ENR no). JS-rendered or Akamai pages, issuer-specific URL patterns that change yearly | — | — | L, high maintenance | last resort for a handful of DE names | — |

**UK: Companies House free Accounts Data Product.** ✓ tested; **useless for listed groups**.
- Daily zips are 52–598 MB. We tested `Accounts_Bulk_Data-2026-08-07.zip`: 7,393 iXBRL files.
- In a 1,849-file sample: 832 micro-entity, 661 small, 335 dormant. Median employees 1, maximum turnover £14.5m. **0 files had `ifrs-full` facts.** None of the 9 UK gap company numbers appeared.
- Large plc group accounts (Tesco `AA`) are **PDF-only** at Companies House.

The UK gap is fully closed by F1 and F2 (FCA NSM and filings.xbrl.org).

---

## 4. Asia

### 4.1 Korea: 13 gap names (all fillable)
| Option | Status | Detail | History | PIT | Effort |
|---|---|---|---|---|---|
| 1. **DART 재무정보 일괄다운로드** (bulk) | ✓ **no key, no login** | List: `POST https://opendart.fss.or.kr/disclosureinfo/fnltt/dwld/list.do`. 172 zips: FY2015, then 2016Q1 to 2026H1, each split into BS, PL, CF and CE. Download: `GET https://opendart.fss.or.kr/cmm/downloadFnlttZip.do?fl_nm=2026_2Q_PL_20261008033244.zip`. cp949 TSV, about 2,100 companies per file. Columns: `재무제표종류, 종목코드, 회사명, 시장구분, 업종, 결산월, 결산기준일, 보고서종류, 통화, 항목코드 (ifrs-full_Revenue / dart_OperatingIncomeLoss / entity…), 항목명, 당기 3개월, 당기 누적, 전기…`. Example: Samsung SDI H1-2026 revenue ₩7,345.2bn | **2015→**, quarterly | **None in file.** Files are regenerated, so values are latest-amended. Join to DART filing dates: free OpenDART `list.json`, or keyless `POST https://dart.fss.or.kr/dsab007/detailSearch.ax` (gives `rcpNo` and date) | M. Downloads are **throttled to about 10 KB/s** (3.8 MB took 6 min), so the backfill of about 170 files takes hours, once |
| 2. OpenDART API (free key, already planned) | D | `fnlttSinglAcntAll` / `fnlttMultiAcnt` with `rcept_no` | 2015→ | `rcept_no` date | S once the key is set |
| 3. SEC FSDS (F3) | ✓ | KB, Shinhan, POSCO 20-F | 2009→ | `filed` | S |

### 4.2 Hong Kong: 10 gap names (all fillable)
| Option | Status | Detail | History | PIT | Effort |
|---|---|---|---|---|---|
| 1. **AKShare `stock_financial_hk_report_em(stock, symbol=利润表/资产负债表/现金流量表, indicator="报告期")`** (Eastmoney HK F10: `datacenter.eastmoney.com/securities/api/data/v1/get`, `RPT_HKF10_FN_{INCOME,BALANCE,CASHFLOW}_PC`) | ✓ 1.1–2.2 s per call | Long format: `SECUCODE, REPORT_DATE, DATE_TYPE_CODE (001 annual / 002 interim / 003-004 quarterly), FISCAL_YEAR, STD_ITEM_CODE, STD_ITEM_NAME, AMOUNT`. Example: Xiaomi H1-2026 revenue HK$/RMB 208.06bn. **No currency column.** Get `CURRENCY` from `RPT_HKF10_FN_MAININDICATOR`; TTI is shown in HKD although it reports in USD | 3690 from 2015, 1810 from 2015, 0388/0883 from 1999, 3988 from 2003, the rest from 2000 | **No notice date.** Synthesize it from the HKEXnews titleSearch results-announcement `DATE_TIME` (already integrated) | S–M (hard timeout wrapper) |
| 2. Sina HK HTML `stock.finance.sina.com.cn/hkstock/finance/03690.html` | ✓ | GBK HTML, latest 8 periods, HKD millions; has `公告日期` | 2 years | weak | S, fallback only |
| 3. Tencent `web.ifzq.gtimg.cn/.../getFinReport` | ✗ | `{"code":21,"msg":"interface offline"}` | — | — | — |
| 4. Futu OpenAPI / Webull | D | needs OpenD plus a brokerage account; not worth it | — | — | — |
| 5. Aastocks / ET Net | scrape only | strict terms | — | — | — |

HKEX publishes no XBRL, only PDFs. Eastmoney data is for personal use (ToS).

### 4.3 Taiwan: 2330 (via F3) and 2317, plus every TWSE/TPEx name
| Option | Status | Detail | History | PIT | Effort |
|---|---|---|---|---|---|
| 1. **MOPS bulk IFRS XBRL** | ✓ | `https://mopsov.twse.com.tw/server-java/FileDownLoad?step=9&functionName=show_file2&fileName=tifrs-2026Q2.zip&filePath=/ifrs/2026/` (111 MB, 2,704 iXBRL files, one per company; `tifrs-fr1-m1-ci-cr-2330-2026Q2.html`, where `cr` = consolidated). Seasons `tifrs-2013Q1…2026Q2`, plus old GAAP `tw-gaap-2009Q4…2014Q4`. Parsed 2330 Q2-2026 `ifrs-full:Revenue` 1,270,380,250 (NT$ thousand), EPS 27.25 | **2013→** quarterly (IFRS), 2009→ old GAAP | Use **upload timestamps** from `https://doc.twse.com.tw/server-java/t57sb01?step=1&co_id=2330&year=114&mtype=A` (e.g. `114/05/15 13:40:15`) | M (about 54 zips × 100 MB, once) |
| 2. MOPS per-company `POST https://mopsov.twse.com.tw/mops/web/ajax_t164sb04` (IS), `…sb03` (BS), `…sb05` (CF) | ✓ | HTML tables, `co_id=2330&year=115&season=02`. The new `mops.twse.com.tw` blocks bots ("FOR SECURITY REASONS"); `mopsov` works | 2013→ | as above | S |
| 3. TWSE OpenAPI `https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci` (IS), `t187ap07_L_ci` (BS), `t187ap14_L` (EPS), **`t187ap05_L` monthly revenue**; TPEx `https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci` | ✓ | **Latest quarter only** (snapshot, no history, no cash flow). `出表日期` is the build date | none | weak | S, a daily incremental check |
| **Monthly revenue** (TW-specific PIT signal) | ✓ | `https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{rocY}_{m}_0.html` (`otc` for TPEx), 2013→ | 2013→ | due by the 10th of M+1 | S |

Licence: Taiwan government open data (OpenAPI); MOPS public.

### 4.4 India: RELIANCE, TCS (and any NSE name)
| Option | Status | Detail | History | PIT | Effort |
|---|---|---|---|---|---|
| 1. **NSE Integrated Filing (results)** | ✓ plain `requests` with a browser UA; **no cookie needed** at ≤1 req/s | `https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=RELIANCE&period_ended=all&type=Integrated%20Filing-%20Financials`. Fields `qe_Date, consolidated, audited, broadcast_Date ("17-Jul-2026 19:50:03"), xbrl, ixbrl`. Parsed XBRL: Reliance FY26 `RevenueFromOperations` ₹10,756.75bn, `ProfitLossForPeriod` ₹957.54bn, EPS 59.69 | Mar-2025→ (results moved here) | **Excellent** (broadcast time to the second) | M |
| 2. **NSE legacy results** | ✓ | `https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=RELIANCE&period=Annual|Quarterly`: 39 annual and 130 quarterly rows from **FY2005** to Dec-2024. Fields `filingDate, broadCastDate, xbrl` (XBRL from about FY2019). Older: `…/corporates-financial-results-data?…&params=…&seq_id=…` JSON (FY16 consolidated net sales ₹27,654,372 lakh) | **FY2005→** | Excellent | M |
| 3. BSE API (`api.bseindia.com/BseIndiaAPI/api/…` with `Referer/Origin: https://www.bseindia.com/`) | ✓ | `FinancialResult/w?scripcode=500325` (FY2014→ zips), `AnnSubCategoryGetData` (with `DT_TM`) | FY2014→ | Good | M |
| 4. MCA21 (AOC-4 XBRL) | D | login plus a per-company fee | — | — | skip |

Pitfalls:
- The legacy XBRL context periods can be mislabelled; use the `DateOfStartOfReportingPeriod`/`DateOfEndOfReportingPeriod` facts.
- Balance sheet and cash flow are semiannual only.
- NSE/BSE site terms prohibit automated scraping. Akamai was permissive at low rate, but this is fragile.

### 4.5 Japan
- **Annual and half-year statements:** EDINET is already integrated. Fix F4 backfills about 10 years.
- **Q1/Q3: TDnet 決算短信 XBRL.** ✓ Verified.
  - Zip `https://www.release.tdnet.info/inbs/{id}.zip` (60–120 KB) holds `XBRLData/Summary/tse-…-ixbrl.htm` plus `Attachment/` BS/PL/CF iXBRL. **The inline-XBRL namespace is `http://www.xbrl.org/2008/inlineXBRL`.**
  - Parsed Fast Retailing FY Aug-2026 (posted 2026-10-08 15:31):
    - `SalesIFRS` ¥3,963.4bn
    - `OperatingIncomeIFRS` ¥743.1bn
    - EPS 1,768.08
    - **next-year forecast** (`…ForecastMember`): sales ¥4.45tn
  - Seven & i Q2 and an Altech Q3 (`QuarterlyPeriod=3`) were also parsed.
  - PIT: list time (JST, to the minute) plus `FilingDate`.
  - **Retention is 31 days, so start the daily scraper now; it can never backfill.** Load is ≤20 min/day at 1 req/s. Effort M. Personal-use viewing-service terms.
- Paid alternative: J-Quants (§6).

### 4.6 China (A-shares covered; H-share-only names are under HK)
| Option | Status | Note |
|---|---|---|
| AKShare Sina `stock_financial_report_sina` | ✓ (ICBC: 90 periods 1998→2026-06, 3 s) | **PIT trap:** `公告日期` is the date of the *latest* report containing that period (restated comparatives), e.g. FY2023 shows 2025-03-29. Use BaoStock `pubDate` or Eastmoney `NOTICE_DATE` for PIT |
| CNINFO webapi (`webapi.cninfo.com.cn/api/stock/p_stock2300`) | D | `401 code_005_ipban_notoken`; needs registration plus an OAuth2 client_credentials token |
| SSE XBRL (`listxbrl.sse.com.cn`), `xbrl.cninfo.com.cn`, SZSE `ShowReport` 1803 | ✗ | retired (301 to a JS app), DNS fails, 403/404 |
| Tushare Pro 2000 pts | D | about ¥200/yr; `ann_date`/`f_ann_date` (good PIT); **companies pay 10×** |

---

## 5. LATAM, Canada, Australia, Singapore

**Correction to `DATA_SOURCES_AMERICAS_EUROPE.md` §8.2.** Mexico, Chile and Peru **do** have free, keyless, machine-readable per-issuer statements with filing timestamps. None of them is a single bulk zip, but each is scriptable per issuer and period.

| Market | Option (ranked) | Status | Endpoint / how | History | PIT | Effort | Fills |
|---|---|---|---|---|---|---|---|
| **MX** | 1. **BIVA "banco de información" JSON API** (also covers BMV-listed issuers, since CNBV/STIV filings go to both exchanges) | ✓ | Issuer id: `GET https://www.biva.mx/emisoras/banco-informacion/claves?clave=GMEX` → `{"id":1693,"clave":"GMEXICO"}`. Ids: GFNORTE 1680, CEMEX 1786, WALMEX 1656, AMX 1653, FEMSA 1782. Filings: `GET https://www.biva.mx/emisoras/banco-informacion?size=100&page=0&empresa=1693&tipoInformacion=17` (17 = quarterly, 15 = annual). Fields `fechaPublicacion` (epoch ms), `tipoDocumento` ("Información trimestral 4D-2025"), `archivosXbrl[{url,extension}]`. File: `https://www.biva.mx/storage/data-rs-cnbv/ReporteTrimestral_GMEXICO_2025_4D_….xbrl` (8.6 MB, taxonomy `ifrs_mx` with **`ifrs-full` concepts**). Parsed GMEXICO FY2025: Revenue US$18,176.7m; ProfitLoss US$5,717.1m; Assets US$42,115.8m | quarterly **2018→** (34–42 filings per issuer). Items before mid-2018 carry the backfill date 2018-10-23, so they are not PIT | **Good** (`fechaPublicacion`) | S–M | GMEXICOB, CEMEXCPO; upgrades AMXB, FEMSAUBD, WALMEX. **GFNORTE** (financial group): no type-17 entry; annual report XBRL (type 15, 2017–2025) has 3-year selected `ifrs-full` figures (FY2025 ProfitLoss MXN 58,788m). Quarterly bank statements are PDF only |
| | 2. BMV issuer pages | ✓ (latest quarter only) | `https://www.bmv.com.mx/es/emisoras/informacionfinanciera/GMEXICO-6008-CGEN_CAPIT` → `/docs-pub/ifrsxbrl/ifrsxbrl_<id>_2026-02_1.zip` (AbaxXBRL JSON `HechosPorId`). Full history is sold ("Publicaciones en línea") | 1 quarter | Good | S | incremental cross-check |
| **CL** | **CMF per-issuer IFRS XBRL** | ✓ | `POST https://www.cmfchile.cl/institucional/mercados/entidad.php?mercado=V&rut=93007000&tipoentidad=RVEMI&vig=VI&control=svs&pestania=3` with form `mm=03|06|09|12, aa=YYYY, tipo=C, tipo_norma=IFRS`. The response links `safec_ifrs_verarchivo.php?auth=…` for the "Estados financieros (XBRL)" zip (`ifrs-full` plus `cl-ci`). SQM FY2024 Revenue US$4,528.8m. **Quirk:** about half the POSTs return a 302 with an empty body; retry after 1–2 s | quarterly **2009→** (Chilean GAAP before) | **Good**: `bitacora_ifrs.php?rut=…&periodo=202412&tb=C` gives a filing log ("05/03/2025 00:32 Primer Envío") | M | SQM-B (also via F3); any Chilean issuer by RUT |
| **PE** | **SMV SIMV** (`Frm_InformacionFinanciera`, ASP.NET postbacks; send "TODOS" as `-1` or you get HTTP 500) | ✓ | Company ids: Credicorp 18049, BCP 26, Buenaventura 50, Alicorp 73, Southern Copper 18050. Grid has **Fecha de Presentación** (e.g. 27/03/2025 18:51:45), structured statements (`Frm_DetalleInfoFinanciera.aspx`) and PDF/XBRL (`ConsultasP8/documento.aspx?vidDoc=`). Credicorp FY2025 net profit S/ 7,082.7m, filed 31-Mar-2026 | 1999 dropdown; structured statements verified 2012→ | **Excellent** | M | BAP (also via F3); any Peruvian issuer. Credicorp switched its reporting currency from USD to PEN |
| **CO** | 1. SFC SIMEV2 REST (`https://www.superfinanciera.gov.co/sfcservices/SIMEV2`, with the `api-key` header that is embedded in the site's public JS) | partial / ✗ | Issuer and period lookups work (Ecopetrol `rnCotien 260, rnCodent 036`, years 2000–2026), but data endpoints (`informes-financieros-Niff/reporte`, `getReporteBg`) returned `[]` or 503 | — | — | L | Ecopetrol: use F3 (FSDS has its FY2025 20-F) |
| | 2. datos.gov.co Supersociedades (`pfdp-zks5`, `prwj-nzxa`, `ctcp-462n`) | ✓ but irrelevant | Securities issuers report to SFC, not Supersociedades: `nit=899999068` → `[]` | — | — | — | none |
| **AR** | CNV AIF | not researched | no gap names; EDGAR 20-F for ADRs | | | | |
| **CA** | SEDAR+ | ✗ | Radware block page (captcha); paid CSA DDS only. All 4 CA names are covered by EDGAR | | | | |
| **AU** | **ASX announcements (PDF)**; no XBRL in AU | ✓ | `https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode=CSL&timeframe=Y&year=2025` (date/time, headline, `idsId`). `displayAnnouncement.do?display=pdf&idsId=…` → hidden `pdfURL` → `https://announcements.asx.com.au/asxpdf/20250819/pdf/….pdf`. The Markit API only returns the latest 5 items | PDFs **2008→** | **Excellent** (announcement time) | PDF route (§8) | CSL, CBA (PDF only). Click-through ASX terms |
| **SG** | SGX announcements API | ✗ | `api.sgx.com/announcements/v1.1` returns 403 (Akamai), then 401 with a browser fingerprint (site token needed). ACRA BizFinx is private companies and paid | — | — | — | D05: DBS IR quarterly PDFs (`dbs.com/iwov-resources/images/investors/quarterly-financials/2026/2Q26_performance_summary.pdf`) |

---

## 6. Paid APIs (about €50/month class)

None of these could be tested with non-US data without signing up.
- **EODHD:** the public `demo` token works for AAPL.US and MCD.US. `BMW.XETRA` and `7203.TSE` return 403.
- **Alpha Vantage and Twelve Data:** the `demo` key works for US only.

| Vendor / plan | Price per month | Non-US statements | History | Filing date / PIT | Rate | Licence for a bank employee | Status |
|---|---|---|---|---|---|---|---|
| **EODHD "Fundamentals" (Equity Analyst)** | **€59.99, or €49.99 billed annually**. All-in-one €99.99 | **Yes**, 60+ exchanges | US from 1985 (demo: AAPL 41 years, 164 quarters). Non-US majors from about 2000; "minor companies" 6–10 years | **`filing_date` on every period** (real from about 1994 for the US; unverified for non-US) plus `Earnings.History.reportDate`; no as-first-reported vintages | 100k calls/day; fundamentals cost 10 calls each | **Non-Professional only. Employees acting for a business are "Professional".** Internal Use is **€399/month**, 1 seat, *no download to a corporate server* | ✓ demo (US only) |
| FMP **Ultimate** (Starter/Premium are US/UK/CA only) | $139, or $99 billed annually | Yes (partner FinancialReports, 50+ regulators) | 30+ years | `filingDate`, `acceptedDate` | 3,000/min | Personal non-business use only; commercial needs an agreement; delete all data on termination | D (no demo key, 401) |
| Finnhub Fundamental-1 | about **$50 per market** per month (×9 markets) | Yes, per market | 10 years, 40 quarters | standardized endpoint is period-based only | 300/min | Personal plan cannot be used by a business | D |
| Twelve Data Pro | $229 | Yes | 6 years (demo); full history on Ultra $999 | **None** | 100 credits per symbol | personal / non-commercial | ✓ demo (US) |
| Alpha Vantage | $49.99+ | **No** (SEC filers only) | 20 years | None | 75+/min | — | ✓ demo (IBM) |
| SimFin / Tiingo / Intrinio / Marketstack / Massive (Polygon) / Financial Datasets / Sharadar | $15–$999 | **No** (US/SEC only) | — | — | — | Tiingo has an internal-commercial tier at $50 (US only) | D |
| Tushare Pro (CN; HK is an add-on of about ¥500/yr) | about ¥200/yr | CN A-shares, HK | full / HK from 2000 | A-share `ann_date`, `f_ann_date` | 200/min | **companies pay 10×** | D |
| J-Quants Light / Standard / Premium (JP) | ¥1,650 / ¥3,300 / ¥16,500 | JP only | 5 / 10 / 20 years. **Light and Standard are summary (tanshin) only**; full statements need Premium | `DiscDate`/`DiscTime` (excellent) | 60–500/min | individual plans; Enterprise separate | D |

**Best value at about €50/month: EODHD Fundamentals, annual billing (€599.90/yr).** It is the only option at this price that standardizes IS/BS/CF across EU, UK, CH, DE, HK, KR, TW, IN, AU, SG and MX with a filing date. Integration is S (one JSON shape: `Financials.{Income_Statement,Balance_Sheet,Cash_Flow}.{yearly,quarterly}`), and about 10k fundamentals pulls per day covers the universe 40×.

Before paying:
1. **Licence.** Use the personal plan only if BiG Equity 360 is genuinely a personal project on a personal machine. For bank use, budget the Internal Use licence (€399/month), or use the bank's institutional feeds (§7).
2. **Verify non-US `filing_date` completeness and depth** in month 1 for 10 test names (BMW, NESN, 005380, 2317, RELIANCE, CBA, D05, GMEXICOB, 0388, ROG).
3. Keep official sources as primary and EODHD as fill and cross-check. EODHD has no restatement vintages.

---

## 7. Institutional sources (BiG)

None are installed or running on this laptop: no bbcomm on port 8194, no Workspace proxy on 9000/9060, no TWS/IB Gateway. The `lseg` plugin and the S&P Global and IBKR connectors are present but **need user authorization** (claude.ai connector settings or `/mcp`). This session is non-interactive and could not authorize them.

| Source | Python access | Batch integration | Coverage / PIT | Licensing for local storage |
|---|---|---|---|---|
| **FactSet** | `fds.sdk.FactSetFundamentals` 3.1.0. `https://api.factset.com/content/factset-fundamentals/v2`: `/fundamentals`, `/periods`, **`/point-in-time`** (POST, "as known on date"). 10 req/s | **Server-friendly** (API key or OAuth) | global, deep, true PIT | enterprise contract. **Best fit if BiG has API entitlement** |
| **S&P Capital IQ** | `spgmiciq` 3.0.0: **`get_income_statement_pit(identifiers, properties={"asOfDate":…})`** and `get_income_statement_historical`. Also Xpressfeed/Compustat Global, Marketplace/Snowflake | Server-friendly with API entitlement (a CIQ Pro desktop seat does *not* include API rights) | 180k+ companies, PIT | enterprise contract |
| **Bloomberg** | `blpapi` (Bloomberg's pip index) plus `xbbg` 1.5.1. Example: `blp.bdh('BMW GY Equity', ['SALES_REV_TURN','NET_INCOME','BS_TOT_ASSET','CF_CASH_FROM_OPER'], '2015-01-01', today, Per='Y')`. PIT via `FILING_STATUS=OR` (as originally reported) and the `ANNOUNCEMENT_DT` field (confirm in FLDS) | **Desktop API only on a PC with a logged-in Terminal**, so a scheduled task on the bank PC. Daily and monthly data limits | global, 20–30+ years | Desktop data is for the entitled user and device only: **no shared or persisted database, no redistribution.** Server use needs B-PIPE/Server API or **Data License** ("Company Financials … Point-in-Time", 33 years, Parquet) |
| **LSEG Workspace** | `lseg-data` 2.1.1. Example: `ld.get_data(rics, ['TR.F.TotRevenue','TR.F.PeriodEndDate','TR.F.OriginalAnnouncementDate'], {'SDate':0,'EDate':-9,'Period':'FY0','Frq':'FY'})` | Desktop session needs Workspace running (about 5 req/s, daily caps); a platform session needs a separately licensed app key | Worldscope global; `OriginalAnnouncementDate` gives event-time PIT | "Licensed for individual use… may not run on a server or redistribute". A local personal cache is a grey area; a shared tool needs a non-display or enterprise licence |
| **Interactive Brokers** | — | — | — | ✗ **Dead.** TWS API 10.47 release notes (2026): "De-Supported Fundamentals Data Request" (`reqFundamentalData` and tick 47 removed). `ReportsFinStatements` already stopped returning data in late 2024. Web API fundamentals tags were deprecated on 2026-01-06 |

**Recommended institutional pattern:**
- Ask BiG market-data whether the firm has **FactSet or CIQ API** entitlement. If yes, build one adapter (`source_id=factset` or `ciq`) as a top-precedence source for DE, CH, AU, SG, MX and for PIT history everywhere.
- If only Bloomberg or LSEG desktop is available, use them for **ad-hoc validation and gap spot-checks**, not as a persisted pipeline source, unless market-data confirms the licence allows it.

---

## 8. PDF annual-report table extraction (no LLM)

✓ Live-tested with pymupdf (pdfplumber is also installed in scratch), using a generic rule-based extractor (`latam_other/pdfx.py`).

**How it works:**
1. **Find the primary statement page.** Look for an EN/ES heading on a page with at least 25 thousands-formatted numbers. Skip "Note N" and contents pages, and take the *earliest* qualifying page.
2. **Rebuild rows** from word boxes using 3 pt y-buckets. Rotate landscape pages.
3. **Split each row** into (label, numbers) segments. This handles side-by-side balance sheets, 3-year layouts and Group/Bank columns. Drop note references.
4. **Parse numbers and units.** `(1,234)` is negative, `–` is 0, and both `1.234.567` and `1,234,567` are accepted. Detect units (`US$m`, `$M`, `S/(000)`).
5. **Map labels** with regex synonyms. The current year is the first number and the prior year the second.

| Document | Revenue | Net profit | Total assets | Time |
|---|---|---|---|---|
| CSL FY25 annual report (151 pp, US$m) | 15,035 / 14,259 | 3,136 / 2,714 | 39,404 / 38,022 | 1.5 s |
| CSL FY15 Appendix 4E (rotated landscape) | 5,458.6 / 5,334.8 | 1,379.0 / 1,307.0 | 6,401.0 / 6,277.7 | 0.4 s |
| Credicorp 2024 (SMV PDF, 180 pp, Spanish, S/(000)) | n/a (bank) | 5,623,252 / 4,959,878 | 256,088,940 / 238,840,188 | 1.7 s |
| CBA AR 2025 (456 pp, $M) | n/a (bank) | 10,133 / 9,481 (continuing operations) | 1,353,799 / 1,254,076 | 7.3 s |

**Honest assessment:**
- The table above is the result after about **6 rounds of rule fixes on these same 4 documents**. The first version got about half right: it picked segment notes or parent-entity pages, took the wrong column in 3-year tables, matched "Total activos financieros", and missed wrapped labels and rotated pages.
- **Estimate for about 30 issuers × 10 years (about 300 PDFs):** under 1 hour of compute.
  - Expect **about 85–90% automatic hits for 3–5 headline lines**. That assumes per-issuer synonym and page overrides, at 15–30 minutes per issuer.
  - Banks and insurers need their own item sets.
- **Validation is essential and cheap:**
  - Compare to the prior-year column of the next year's report.
  - Check that assets equal liabilities plus equity.
  - Check units.
- **Effort:** headline lines only is **M**. Full statements mapped to our canonical items is **L**, and needs ongoing upkeep as layouts change.
- **Use it only where nothing structured exists:** CSL, CBA, DBS, GFNORTE quarterly, and the CH/DE names if no paid or institutional source is adopted. **Never use it as the primary source where XBRL exists.**

---

## 9. Coverage-gain estimate (MVP global universe, 248 names; 112 gaps)

Each gap name is assigned to the cheapest source that gives an official or structured latest FY with PIT. The count is cumulative.

| Step | Source | Gap names filled | Names | Cumulative latest FY, non-yfinance (incl. upgrades of yfinance-only names) |
|---|---|---|---|---|
| now | — | — | — | **121 / 248** (136 incl. yfinance) |
| F1+F2 | ESEF per-LEI plus LEI fix | 32 | EU 21 (DSV MAERSK-B KNEBV NDA-FI CS DG DSY ENGI HO RI RMS SAF SU BNP G AD ADYEN AIR ASM WKL HEIA), UK 11 (all) | 153 |
| F3 | SEC FSDS (IFRS 20-F) | 9 | 2330, 105560, 055550, 005490, SQM-B, ECOPETROL, CEMEXCPO, BAP, TEF | 162 |
| F4 | EDINET backfill | 2 | 3382, 9983 | 164 |
| A1 | DART bulk (no key) / OpenDART | 10 + 2 upgrades | 006400 373220 207940 068270 051910 012330 035420 035720 000270 005380; upgrades 005930, 000660 | 176 |
| A2 | Eastmoney HK (AKShare) | 10 + 4 upgrades | 2020 3690 3988 0883 1810 0388 0016 0669 0002 0001; upgrades 0700 0939 0941 1299 | 190 |
| E1 | DILA France | 6 | AI BN KER MC EL ORA | 196 |
| E2 | Nasdaq Nordic + NewsWeb + STORI + AFM | 7 | ATCO HM INVE VOLV, DNB, UCB, PRX | 203 |
| E3 | eMarket Storage / 1Info | 3 | ENEL ISP UCG | 206 |
| E4 | CNMV IFA / IPP | 2 + 2 upgrades | AMS, CABK; upgrades IBE, ITX | 210 |
| A3 | MOPS (TW) | 1 | 2317 | 211 |
| A4 | NSE (IN) | 2 | RELIANCE, TCS | 213 |
| L1 | BIVA (MX) | 2 + 3 upgrades | GMEXICOB, GFNORTEO (annual only); upgrades AMXB, FEMSAUBD, WALMEX | **218** |
| **Remaining: 30 names without a free structured source** | | | **DE 16** (BMW VOW3 ADS BAS ENR RHM MUV2 MBG EOAN BAYN DB1 IFX DHL DTE ALV SIE); **CH 9** (NESN ROG RO SIKA ZURN HOLN LONN CFR ABBN); **PT 2** (EDP, GALP: ESEF only to FY2024); **AU 2** (CSL, CBA); **SG 1** (D05) | yfinance (F5) now. Then EODHD / FactSet / CIQ, or PDF (AU/SG) |

Optional duplicates add redundancy, not names: CMF Chile (SQM) and SMV Peru (BAP) give PIT-dated quarterly data back to 2009/2012 alongside FSDS.

Counting "upgrades" (names that today only have yfinance), the free tiers reach about **218 of 248** global names (88%) with a structured, PIT-dated latest FY. All are official regulator or exchange sources except HK (Eastmoney). With yfinance as fallback, **248/248** have a latest FY.

**10-year history after the free tiers:**
- **10y+ by default:** US (SEC), BR (CVM), CN (BaoStock/Sina), JP after F4, KR (DART bulk 2015→, so 11y), TW (2013→), IN (FY2005→), HK (Eastmoney 2000→), ES semiannual (2008→), and the 20-F filers (FSDS 2009→).
- **ESEF countries (FR, NL, IT, Nordics, BE, UK):** only FY2020→, about 6 years. Older years exist only as PDFs or in paid data. **That is the main remaining argument for a paid or institutional source in Europe.**

---

## 10. Recommended plan

### Tier 1: free, now
1. **Pipeline fixes F1–F5** (§2). These are about 2–3 days of work, for +43 official names plus latest-FY coverage for all 248.
2. **Start the TDnet tanshin daily scraper now.** History accrues only from the start date.
3. **DART bulk backfill** (one-off, about 170 zips, slow server) plus OpenDART key for filing dates. Set the key: the user already knows it is free.
4. **Eastmoney HK statements via AKShare** (in a hard-timeout worker), with PIT from HKEXnews result announcements.
5. **EU OAM adapters, in order:** DILA France (S) → Nasdaq Nordic news (S) → NewsWeb (S) → FSMA STORI (S) → AFM export (S) → eMarket/1Info (S–M) → CNMV IFA and IPP (M). All feed the same Arelle → xBRL-JSON → `_xbrl_json.py` path.
6. **Taiwan MOPS** bulk XBRL plus upload timestamps; **NSE** integrated-filing and legacy results.
7. **LATAM:** BIVA (MX) adapter (S–M). CMF (CL) and SMV (PE) are optional, for quarterly PIT history beyond FSDS.
8. Leave **DE, CH, PT, AU, SG** (30 names) on yfinance until ESAP (Jul 2027) or a paid/institutional source. Optionally, run PDF headline extraction for CSL, CBA and DBS.

### Tier 2: about €50/month
- **EODHD Fundamentals (€49.99/month billed annually), if and only if the project is personal and non-professional.**
  - Use it to fill DE, CH, AU, SG, MX and PT, and to extend ESEF countries back to about 2000–2015, with `filing_date` as PIT.
  - Keep official sources at higher precedence. Use EODHD for gap-fill and reconciliation.
- If the tool is used for BiG work, a €50 plan is **not licence-compliant**. Go to Tier 3, or EODHD Internal Use at €399/month (1 seat, no corporate-server download).
- Optional, Japan only: J-Quants Light (¥1,650) gives a tanshin summary history of 5 years, which replaces the "accrue from today" limitation of the TDnet scraper.

### Tier 3: institutional (BiG)
- **First choice:** FactSet Fundamentals API or S&P CIQ API (`spgmiciq`) under BiG's enterprise licence. Both are server-friendly, true PIT (`/point-in-time`, `get_*_pit`), global and 20+ years.
  - Implement as one adapter with top precedence for DE, CH, AU, SG and MX and for pre-2020 European history.
- **Bloomberg/LSEG desktop:** use for validation and ad-hoc fills only (a scheduled job on the Terminal/Workspace PC), unless market-data confirms that persisted internal storage is allowed. A Bloomberg Data License PIT dataset is the clean route at enterprise cost.
- **Authorize** the `lseg` plugin and the S&P Global connector (claude.ai connector settings or `/mcp`) to let Claude query them interactively for spot checks.

---

## 11. Top 5 actions by coverage gain per effort

| Rank | Action | Effort | Gain |
|---|---|---|---|
| 1 | **Run `yfinance_fund` for all 248 non-US names** (F5) | S (config/run) | **+112 names latest FY** (no PIT, 4–5 years). Immediately closes the "missing" view |
| 2 | **ESEF per-LEI run plus GLEIF fix for 11 missing LEIs** (F1+F2) | S | **+32 names** official FY2025 with ESEF history from FY2020 (UK 11/11, EU +21) |
| 3 | **SEC FSDS loader for 20-F IFRS filers** (F3) | S–M | **+9 names** (TSMC, KB, Shinhan, POSCO, SQM, Ecopetrol, Cemex, Credicorp, Telefónica) with 2009→ history and `filed` dates, and protects every IFRS FPI against companyfacts lag |
| 4 | **DART bulk + Eastmoney HK** | M + S–M | **+20 names** (+6 upgrades): KR 15/15 and HK 24/24, both with 10+ years |
| 5 | **DILA France + Nasdaq Nordic/NewsWeb/STORI/AFM adapters** (Arelle shared) | S each (about 4 days total) | **+13 names** (FR 16/16, SE/NO/BE/NL complete) with exact publication timestamps |

Next in line:
- EDINET backfill (+2 names and 10 years of JP history; S).
- Start the TDnet daily scraper (Q1/Q3 for 40 JP names; M; time-critical).
- eMarket (+3), CNMV (+2/+2), BIVA (+2/+3), MOPS (+1), NSE (+2).

---

## 12. Dead, blocked or not worth it

| Source | Finding |
|---|---|
| IBKR TWS API fundamentals | **Removed in TWS API 10.47 (2026)**; data stopped in late 2024; Web API fundamentals tags deprecated on 2026-01-06 |
| Companies House Accounts Data Product | Works, but small and micro companies only. **0 `ifrs-full` files**; plc group accounts are PDF |
| SEC companyfacts / frames for IFRS FY2025 | **Missing**. Use FSDS |
| SEDAR+ (CA) | Radware captcha block; paid DDS only |
| SGX announcements API | 403 Akamai, then 401 (site token) |
| Colombia SFC SIMEV2 data endpoints | lookups work, data returns `[]` or 503; datos.gov.co Supersociedades excludes securities issuers |
| BMV history | latest quarter only; history is sold. Use BIVA |
| Germany Unternehmensregister / Bundesanzeiger | captcha per publication, no API |
| Portugal CMVM | OutSystems SPA, `No role validation found` |
| Denmark Finanstilsynet OAM | submission SPA, no public search |
| CNINFO webapi | token required (`code_005_ipban_notoken`) |
| SSE XBRL platform, xbrl.cninfo.com.cn, SZSE 1803 | retired, DNS fail, 403/404 |
| Tencent HK finance API | `interface offline` |
| `mops.twse.com.tw` (new MOPS) | bot block; use `mopsov.twse.com.tw` |
| TWSE/TPEx OpenAPI statements | latest quarter only (snapshot, no history) |
| Twelve Data, Alpha Vantage, SimFin, Tiingo, Intrinio, Marketstack, Massive | no or weak non-US statements, or no filing date |
| FMP / EODHD / Finnhub personal plans | **not licensed for use by a bank employee for business** |
| IR-site ESEF discovery | not scalable (JS/Akamai, per-issuer patterns, many issuers do not post ESEF) |
| MCA21 (India) | login plus fee per company |
