# BiG Equity 360: Data Sources for Asia Equities, Global Macro and Events/News/Sentiment

*Research date: 2026-10-10. Verified against official docs and with live calls from the target machine (Windows 11, Python 3.13.7, Portugal residential IPv6). Anything marked **(live-tested)** was called from this box on 2026-10-10. Anything marked **(doc-only)** comes from official documentation and was not exercised, usually because a key is needed.*

> Status of `.env`: the file exists but is **empty (0 bytes)**, so no FRED key was available. FRED series IDs were checked through the keyless `fredgraph.csv` endpoint, which shows the same series catalogue. No secrets were read, printed or stored.

---

## 0. TL;DR

| Region / class | Primary (free) | Fallback | Status |
|---|---|---|---|
| **Japan** filings and fundamentals | **EDINET API v2** (free key): annual (120) and half-year (160) reports, large-shareholding reports (350/360), XBRL→CSV (`type=5`) | J-Quants Light (paid, ¥1,650/mo) for TDnet *kessan tanshin* numbers | Quarterly reports were **abolished in Apr-2024**. Q1/Q3 numbers now exist only in TDnet tanshin |
| Japan security master | **JPX `data_j.xlsx`** (33/17-sector codes, size class), refreshed monthly | EDINET code list zip (daily) | **live-tested** |
| Japan prices | Yahoo chart API (`7203.T`) | J-Quants free tier is **useless for daily** (12-week delay, 2-year history) | **live-tested** |
| **Korea** filings, fundamentals and insiders | **OpenDART** (free key, 20,000 calls/day): `corpCode.xml`, `fnlttSinglAcntAll`, `fnlttMultiAcnt` (100 companies per call), `elestock`, `majorstock`, `list` | none needed | **live-tested** (endpoint reachable, key error as expected) |
| Korea security master | **KIND corp list** (no key, KSIC industry names) + DART `company.json` (`induty_code` = KSIC) | KRX Open API base-info | **live-tested** |
| Korea prices | **KRX Open API** (free key, 10,000 calls/day, whole market per call, from 2010) | Naver chart endpoints (no key), Yahoo `.KS/.KQ` | KRX Open API reachable (401 without key). `data.krx.co.kr` (pykrx backend) is **login-walled** |
| **China A-share** | **BaoStock** (no key: daily k-line, PE/PB, `pubDate`-stamped profit/growth, CSRC industry) + **AKShare** (Sina/THS/Eastmoney/CNINFO statements, CNINFO announcements and industry) | Tushare Pro (token; free tier gives only unadjusted daily) | BaoStock and AKShare Sina/THS/CNINFO paths worked. **Eastmoney endpoints were flaky or blocked from the EU** |
| **Hong Kong** | HKEX securities list xlsx + **HKEXnews titleSearch JSON** (announcements) + **HKEX DI HTML** (director/substantial-holder dealings) + AKShare `stock_financial_hk_report_em` | Yahoo `.HK`, Sina HK daily | **live-tested**, all usable without keys |
| **Macro (US)** | **FRED/ALFRED** (key, 120 req/min) | `fredgraph.csv` (keyless, latest vintage only) | 140+ IDs verified, 20 dead or stale ones flagged (§5.3) |
| **Macro (international)** | Direct central bank and statistics APIs: **ECB, Eurostat, BoE, BoJ (new 2026 API), BoK ECOS, BCB SGS, Banxico SIE, BIS, IMF SDMX 3, OECD SDMX** | DBnomics (**stale for IMF IFS, BIS and NBS**, see §5.5) | **live-tested** |
| **FX** | **ECB euro reference rates** (zip, 1999→, 29 currencies incl. KRW, JPY, CNY, HKD, BRL, MXN, GBP, INR) | FRED H.10 (adds TWD; weekly lag) + central banks for CLP, ARS, COP, PEN (all keyless and tested) + Yahoo as cross-check | **live-tested** |
| **Events/tone** | **GDELT raw GKG + Events 15-min files** (no key, about 1.4 GB/day zipped) | GDELT DOC 2.0 API for a small watchlist only | DOC API is rate-limited (429s) and **showed a coverage collapse in early Oct-2026** |
| **News sentiment NLP** | `ProsusAI/finbert` (EN) + `cardiffnlp/twitter-xlm-roberta-base-sentiment` (multilingual) | `yiyanghkust/finbert-tone` (needs explicit classes on transformers 5.x) | Benchmarked on CPU (§8) |

**Dead, paywalled or geo-blocked sources found:**
- `data.krx.co.kr` needs a login (pykrx needs `KRX_ID`/`KRX_PW`).
- China NBS `data.stats.gov.cn` returns **403 from the EU**.
- `dataservices.imf.org` (old IMF API) no longer resolves in DNS.
- J-Quants free tier has a 12-week delay.
- Tushare's free tier is close to useless.
- Eastmoney (via AKShare) gave RemoteDisconnected or hangs from this IP.
- FRED: ISM PMI and the LBMA gold price are gone. ICE BofA OAS series are limited to **3 years** of history (since Apr-2026).
- The ECB `ICP` dataset was replaced by `HICP`, and Eurostat `prc_hicp_manr` was replaced by `prc_hicp_minr` (ECOICOP 2, Feb-2026).

---

## 1. Japan

### 1.1 EDINET API v2 (FSA): primary for JP filings
- **Docs:** API spec v2 [ESE140206.pdf](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf). The portal is reachable from https://disclosure2.edinet-fsa.go.jp.
- **Base:** `https://api.edinet-fsa.go.jp/api/v2/`
- **Auth:** a free `Subscription-Key` is mandatory (since the v2 switch). Without it **(live-tested)**: `{"StatusCode": 401,"message": "Access denied due to invalid subscription key..."}`, served with HTTP 200, so **check the JSON `StatusCode`, not only the HTTP code**.
  - Signup: https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1 (name, email, phone with SMS verification using a country code; the key appears in a pop-up, so allow pop-ups). The key does not expire.
  - Env var: `EDINET_APIKEY`.
- **Rate limit:** no published hard number. Run sequentially at about 1 req/s with backoff. Downloads are per document.
- **History:** documents are retained about 10 years (EDINET disclosure retention). Fetch the list day by day.
- **Endpoints:**
  - Document list, one call per calendar day: `GET /documents.json?date=2026-10-09&type=2&Subscription-Key=$EDINET_APIKEY` (`type=1` returns metadata counts only; `type=2` returns the full list).
    - Trimmed response: `metadata{title, parameter{date,type}, resultset{count}, processDateTime, status, message}`, `results[]{seqNumber, docID, edinetCode, secCode, JCN, filerName, fundCode, ordinanceCode, formCode, docTypeCode, periodStart, periodEnd, submitDateTime, docDescription, issuerEdinetCode, subjectEdinetCode, subsidiaryEdinetCode, currentReportReason, parentDocID, opeDateTime, withdrawalStatus, docInfoEditStatus, disclosureStatus, xbrlFlag, pdfFlag, attachDocFlag, englishDocFlag, csvFlag, legalStatus}`.
  - Document retrieval: `GET /documents/{docID}?type=N&Subscription-Key=…` where `type=1` is the original zip (XBRL+HTML), `2` is the PDF, `3` is attachments, `4` is English and `5` is the **XBRL→CSV zip (preferred)**. The CSV only exists when `csvFlag="1"` (taxonomies from 2014 onward).
  - `docTypeCode` values we need:
    - `120` annual securities report (有価証券報告書) and `130` its amendment.
    - `140` quarterly report, **historical only** (abolished for periods starting on or after 2024-04-01).
    - `160` half-year report (半期報告書) and `170` its amendment.
    - `180` extraordinary report.
    - `220` treasury-share buyback status.
    - `350` large-shareholding report incl. change reports (大量保有報告書・変更報告書) and `360` corrections.
    - Verify against the code table in the spec appendix.
- **EDINET code list (bulk):** `https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip` **(live-tested, 573 KB, no key)**.
  - It contains `EdinetcodeDlInfo.csv`, **cp932-encoded, with a metadata first line**, so use `skiprows=1`.
  - 11,410 rows, 3,815 of them listed. Columns: `ＥＤＩＮＥＴコード, 提出者種別, 上場区分, 連結の有無, 資本金, 決算日, 提出者名, 提出者名（英字）, 提出者名（ヨミ）, 所在地, 提出者業種, 証券コード, 提出者法人番号`.
  - The securities code is **5 digits** (4-char code + trailing `0`).
- **Point-in-time:** `submitDateTime` ("YYYY-MM-DD HH:MM" JST) is the knowledge date. Amendments carry `parentDocID`, and `withdrawalStatus` and `disclosureStatus` change later (re-poll recent days).
- **Licensing:** government open data (PDL 1.0 / CC-BY-compatible terms). Attribution is required.
- **Python:** raw `httpx`/`requests` + `zipfile` + `pandas` is enough. Community wrappers (`edinet-tools`, `edinet-xbrl`) exist but none is necessary. Pure Python, so 3.13 is fine.
- **Pitfalls:**
  1. XBRL→CSV files are **UTF-16 LE, tab-separated** (open with `encoding="utf-16"`, `sep="\t"`).
  2. Element IDs follow `jppfs_cor:`/`jpcrp_cor:` taxonomies and change yearly. Map a curated element list (NetSales, OperatingIncome, …) per taxonomy year, and remember IFRS/US-GAAP filers use `jpigp_cor:`.
  3. Late June brings about 2,500 annual reports in roughly a week, so plan a backlog queue.
  4. Since 2024 new TSE codes can be **alphanumeric** (e.g. `130A`), so never store codes as ints.

### 1.2 TDnet (timely disclosure: kessan tanshin, guidance revisions, buybacks)
- **Public viewing service:** `https://www.release.tdnet.info/inbs/I_list_{page:03d}_{YYYYMMDD}.html` **(live-tested)**. 100 rows per page; the page shows `全265件` for 2026-10-09.
  - Columns: `時刻, コード (5-digit), 会社名, 表題, XBRL, 上場取引所, 更新履歴`.
  - Links: PDF `https://www.release.tdnet.info/inbs/{14-digit}.pdf` and XBRL zip `https://www.release.tdnet.info/inbs/{id}.zip`.
- **Retention is only about 31 days**, so it must be scraped **daily** to build history.
- **Point-in-time:** the list date plus `時刻` (JST to the minute), which is excellent for event studies.
- Historical archives and an API are available only through the **J-Quants TDnet add-on (¥11,000/mo)** or paid TDnet DB.
- **Licensing:** TSE copyright, a viewing service with no published API terms. Keep it to low-volume personal use with no redistribution.

### 1.3 JPX listed-company list (TSE 33/17 industry codes)
- The page https://www.jpx.co.jp/markets/statistics-equities/misc/01.html links to **`https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx`**. The old `data_j.xls` URL now returns **404**. **(live-tested)**
- It has 4,447 rows (Prime 1,550, Standard 1,559, Growth 593, plus ETFs, REITs and PRO Market).
- Columns: `日付, コード, 銘柄名, 市場・商品区分, 33業種コード, 33業種区分, 17業種コード, 17業種区分, 規模コード, 規模区分`.
- Updated monthly (3rd business day). Scrape the href from the page in case the attachment folder changes.
- Some JPX pages return 403 to non-browser user agents, so send a browser UA.

### 1.4 J-Quants API (JPX official)
- **Docs:** https://jpx-jquants.com/en/ ([rate limits](https://jpx-jquants.com/en/spec/rate-limits), [V1→V2 migration](https://jpx-jquants.com/en/spec/migration-v1-v2)).
- **V2** (`https://api.jquants.com/v2/`) uses an `x-api-key` header. **(live-tested)** Without a key it returns `403 {"message": "The api key is required."}`. Accounts registered on or after 2025-12-22 can use only V2.
- **Endpoints:** `/v2/equities/master`, `/v2/equities/bars/daily`, `/v2/fins/summary` (tanshin-derived), `/v2/fins/details`, and bulk CSV via `/bulk/list` + `/bulk/get` (paid plans).

| Plan | Price | History | Delay | Rate |
|---|---|---|---|---|
| Free | ¥0 | 2 y | **12 weeks** | 5 req/min |
| Light | ¥1,650/mo | 5 y | none | 60/min |
| Standard | ¥3,300/mo | 10 y | none | 120/min |
| Premium | ¥16,500/mo | ≤20 y | none | 500/min |

- `fins/summary` and `fins/details` have a separate cap of 60/min. Exceeding limits returns 429, and repeated abuse brings a 5-minute block.
- **Recommendation:** skip Free. If Japan Q1/Q3 numbers matter, **Light (¥1,650/mo)** is the cleanest legal source of tanshin fundamentals with `DisclosedDate/DisclosedTime`. Licensing is personal use with no redistribution.
- **Python:** the `jquants-api-client` package exists, but check it targets V2. Otherwise use raw `requests`.

### 1.5 Japan prices
- Yahoo chart API (`https://query1.finance.yahoo.com/v8/finance/chart/7203.T?range=5d&interval=1d`) **(live-tested)** returned OK (JPY, JPX). It is unofficial and against Yahoo's ToS, so use it as a fallback only.
- Polygon/Massive covers US only, and another agent covers that.

---

## 2. South Korea

### 2.1 OpenDART (FSS): primary for KR filings, financials and insiders
- **Docs:** https://opendart.fss.or.kr/guide/main.do
- **Base:** `https://opendart.fss.or.kr/api/`. Every call takes a `crtfc_key` (40 chars).
  - Signup: https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do. Env var: `OPENDART_APIKEY`.
  - **(live-tested)** Without a key: `{"status":"100","message":"인증키가 누락되었습니다."}`.
- **Quota:** **20,000 requests/day per key**. Status `020` means "요청 제한을 초과하였습니다". Status `000` means OK and `013` means no data. Keep bursts modest (≤5–10 req/s), because aggressive bursts are reported to trigger temporary IP blocks.
- **Endpoints:**

| Need | Endpoint | Key params | Notes |
|---|---|---|---|
| corp_code master (bulk) | `corpCode.xml` | – | Zip containing `CORPCODE.xml`: `corp_code (8), corp_name, stock_code (6, blank if unlisted), modify_date`. Download daily |
| Company profile | `company.json` | `corp_code` | `corp_cls (Y/K/N/E), induty_code (KSIC), est_dt, acc_mt (FY-end month), jurir_no, bizr_no, adres, hm_url, …` |
| Disclosure list | `list.json` | `bgn_de, end_de, pblntf_ty (A periodic, B major events, C securities, D shareholding, …), corp_cls, page_no, page_count≤100` | **Without `corp_code` the window is limited to 3 months.** Response: `rcept_no (14), rcept_dt, corp_code, corp_name, stock_code, report_nm, flr_nm, rm` (`rm` flags: 유 KOSPI, 코 KOSDAQ, 연 consolidated, 정 amended, 철 withdrawn) |
| Full financial statements | `fnlttSinglAcntAll.json` | `corp_code, bsns_year (≥2015), reprt_code (11013 Q1, 11012 H1, 11014 Q3, 11011 annual), fs_div (CFS/OFS)` | Rows: `rcept_no, sj_div (BS/IS/CIS/CF/SCE), account_id (IFRS XBRL element), account_nm, thstrm_amount, frmtrm_amount, …` |
| Major accounts, many companies | `fnlttMultiAcnt.json` | `corp_code=` comma list (**≤100 per call**), `bsns_year, reprt_code` | Best for backfill: 2,800 listed companies in about 28 calls per period |
| Original XBRL | `fnlttXbrl.xml` | `rcept_no, reprt_code` | Zip |
| Executive and major-holder ownership (Korean "Form 4") | `elestock.json` | `corp_code` | Full history per company: `rcept_no, rcept_dt, repror, isu_exctv_rgist_at, isu_exctv_ofcps, isu_main_shrholdr, sp_stock_lmp_cnt, sp_stock_lmp_irds_cnt, sp_stock_lmp_rate, sp_stock_lmp_irds_rate` |
| 5% large-holding reports | `majorstock.json` | `corp_code` | Large-holding change history |
| Periodic-report sections | `hyslrSttus` (largest holder), `hyslrChgSttus`, `mrhlSttus` (minority holders), `exctvSttus` (executives), `tesstkAcqsDspsSttus` (treasury shares), `alotMatter` (dividends) | `corp_code, bsns_year, reprt_code` | |
| Document | `document.xml` | `rcept_no` | Zip of the filing (XML/HTML), for text and "Lazy Prices" style analysis |

- **Point-in-time:** `rcept_dt` (date only, no time). The `rcept_no` prefix is also the date. Assume information is available at the **next KRX session open** unless you scrape the time from the DART viewer.
- **History:** filings from about 1999. `fnltt*` structured financials start in **2015**.
- **Licensing:** public data, free use with attribution.
- **Python:** raw `requests` is recommended. `OpenDartReader` and `dart-fss` exist; their heavier dependencies were not tested on 3.13.
- **Pitfalls:** `account_nm` varies by company, so map on `account_id` (`ifrs-full_Revenue`, …) and fall back to the name. Banks and insurers use different statement layouts. Amounts are strings with commas or `-`.

### 2.2 KRX listed list and KSIC industry
- **KIND corp list** (no key) **(live-tested)**: `https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13`.
  - It returns an **HTML table served as `.xls`, EUC-KR encoded**. Parse with `pd.read_html(r.content.decode("euc-kr"))`.
  - 2,801 rows. Columns: `회사명, 시장구분, 종목코드, 업종 (KSIC industry name), 주요제품, 상장일, 결산월, 대표자명, 홈페이지, 지역`.
- Numeric KSIC codes come from DART `company.json.induty_code`.

### 2.3 KRX prices
- **KRX Open API** (official, free key):
  - Portal: https://openapi.krx.co.kr. Register, request an auth key, then click "API 이용신청" for each service (e.g. 유가증권 일별매매정보, 코스닥 일별매매정보, 종목기본정보). Approval takes about 1 day.
  - Env var: `KRX_OPENAPI_KEY`.
  - Endpoint pattern **(live-tested, 401 without key)**: `GET https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd?basDd=20261008` with header `AUTH_KEY: <key>`. KOSDAQ uses `ksq_bydd_trd`; base info uses `stk_isu_base_info` / `ksq_isu_base_info`.
  - **Each call returns the whole market for one date.**
  - Quota is **10,000 calls/day**. Data runs from 2010 and is updated around 08:00 KST the next day.
- **pykrx 1.2.9** works on Python 3.13, with conditions:
  - It now logs into data.krx.co.kr through the `KRX_ID`/`KRX_PW` env vars, because KRX made the data marketplace login-only in Dec-2024.
  - Without login, single-ticker OHLCV still worked **(live-tested via its Naver fallback)**, but `get_market_ticker_list` returned 0 tickers.
  - **pykrx pins `pandas<3.0`**, which **conflicts with the project venv (pandas 3.0.6)**. Avoid it in the main venv, or isolate it in a subprocess or venv.
- **Naver** (no key) **(live-tested)**:
  - `https://api.stock.naver.com/chart/domestic/item/005930/day?startDateTime=202610010000&endDateTime=202610100000` returns JSON with OHLCV plus `foreignRetentionRate`.
  - `https://fchart.stock.naver.com/sise.nhn?symbol=005930&timeframe=day&count=N&requestType=0` returns EUC-KR XML.
  - Both are unofficial.
- Yahoo `005930.KS` works **(live-tested)**.

---

## 3. China and Hong Kong

### 3.1 AKShare (scraper library over Eastmoney, Sina, THS, CNINFO, …)
- **Status:** v1.19.1 (Oct-2026). It requires Python ≥3.11 and is **compatible with Python 3.13 and pandas 3.0.6** (spot-tested in a clean pandas-3 venv). Install: `pip install akshare` (pulls `curl_cffi`, `mini-racer`).
- **Live results from Portugal (2026-10-10):**

| Function | Source | Result |
|---|---|---|
| `stock_info_a_code_name()` | SSE/SZSE/BSE | OK, 5,573 codes, **78 s** |
| `stock_zh_a_daily(symbol="sh600519", adjust="qfq")` | Sina | OK, 4 s, includes `outstanding_share, turnover` |
| `stock_zh_a_hist_tx(...)` | Tencent | OK, 4 s |
| `stock_zh_a_hist(...)`, `stock_hk_hist`, `stock_zh_a_spot_em`, `stock_hk_spot_em`, `stock_board_industry_name_em`, `stock_individual_info_em` | **Eastmoney push2** | **FAIL: RemoteDisconnected / JSON error** (the same host answered a raw `requests` call, so treat it as anti-bot and intermittent) |
| `stock_balance_sheet_by_report_em("SH600519")`, `stock_profit_sheet_by_report_em` | Eastmoney datacenter | OK but **31–35 s per stock** (21 paged calls). 319 and 203 columns with `NOTICE_DATE`, `UPDATE_DATE`, so it is PIT-friendly |
| `stock_financial_report_sina(stock="sh600519", symbol="资产负债表"/"利润表"/"现金流量表")` | Sina | OK, 4 s, 103 periods × 147 columns |
| `stock_financial_abstract_ths(symbol, indicator="按报告期")` | THS | OK, 3.4 s, 25 key metrics |
| `stock_financial_hk_report_em(stock="00700", symbol="资产负债表", indicator="年度")` | Eastmoney HK | OK, 1.3 s (long format) |
| `stock_financial_hk_analysis_indicator_em` | Eastmoney HK | OK, 1.1 s |
| `stock_hk_daily(symbol="00700", adjust="qfq")` | Sina | OK, 2 s, from 2004 |
| `stock_hk_spot()` | Sina | OK, 65 s, 2,815 rows |
| `sw_index_third_info()` | Shenwan | OK (SW L3 industries) |
| `stock_industry_clf_hist_sw()` | swsresearch.com | **SSL CERTIFICATE_VERIFY_FAILED on Windows** (incomplete chain; use `truststore` or download manually) |
| `stock_industry_category_cninfo`, `stock_industry_change_cninfo(symbol, …)` | CNINFO | OK, gives the industry change history per stock (PIT industry) |
| `stock_share_change_cninfo(symbol, …)` | CNINFO | OK, share-capital changes with `公告日期` |
| `stock_ggcg_em` (insider increase/decrease), `stock_hold_management_detail_em` (executive holdings) | Eastmoney | **FAIL after ~2 min (ChunkedEncodingError)**. `stock_hold_management_person_em` **hung for more than 25 min (no timeout in AKShare)** |
| `stock_zh_a_disclosure_report_cninfo(symbol, market="沪深京", category="年报", …)` | CNINFO | OK, announcement list with titles, timestamps and PDF URLs |
| `macro_china_cpi_yearly`, `macro_china_pmi_yearly` | Jin10 / Eastmoney | OK (20–37 s) |

- **Pitfalls:**
  1. AKShare functions have **no timeouts**, so run each in a worker with a hard timeout (e.g. `concurrent.futures` plus process kill).
  2. They print `tqdm` bars.
  3. Symbol formats differ per function (`sh600519`, `SH600519`, `600519`).
  4. Column names are Chinese and change between versions, so pin the version and contract-test daily.
  5. The underlying sites own the data (research and personal use only).
- **Recommendation:**
  - Use Sina for statements (fast) and THS for metrics.
  - Use **Eastmoney by-report only for incremental refreshes** of companies that just filed (it is the only source that carries `NOTICE_DATE`).
  - Use CNINFO for announcements and industry history.

### 3.2 BaoStock (free A-share data, no key)
- **(live-tested)** `baostock 0.9.4` works on Python 3.13 and pandas 3. Calling `bs.login()` logs in anonymously.
- **Daily k-line:** `query_history_k_data_plus("sh.600519", "date,code,open,high,low,close,volume,amount,adjustflag,turn,peTTM,pbMRQ,isST", start_date, end_date, frequency="d", adjustflag="3")`. Data from 1990. `adjustflag`: 1 = hfq, 2 = qfq, 3 = none.
- **Fundamentals with publication date:**
  - `query_profit_data`, which returned `pubDate=2026-04-17, statDate=2025-12-31, roeAvg, npMargin, gpMargin, netProfit, epsTTM, MBRevenue, totalShare, liqaShare`.
  - `query_growth_data`, `query_balance_data`, `query_cash_flow_data`, `query_operation_data`, `query_dupont_data`.
  - `query_performance_express_report` and `query_forecast_report` (preliminary results and guidance).
- **Industry:** `query_stock_industry()` gives the CSRC classification (e.g. `C15酒、饮料和精制茶制造业`) with `updateDate`.
- **Limits:** no published quota. The client is single-connection, so run it sequentially; about 0.1–0.3 s per call was observed. There is no whole-market-by-date endpoint, so expect about 5,500 calls/night for prices (~15–30 min).
- **Gaps:** no HK, no insider data, A-shares only. Personal and research use.

### 3.3 Tushare Pro
- Token from https://tushare.pro/register (env var `TUSHARE_TOKEN`).
- Points system ([doc](https://tushare.pro/document/1?doc_id=290)):
  - **Free (120 pts):** only unadjusted daily bars, 50 calls/min, 8,000/day.
  - **2,000 pts (~¥200/yr):** financial statements, 100/min.
  - **5,000 pts:** 500/min.
  - HK, US and minute data are separate paid products.
- Verdict: only useful if paid. BaoStock plus AKShare cover the free path.

### 3.4 Exchange lists (all **live-tested**, no key)
- **SSE:** `https://query.sse.com.cn/sseQuery/commonQuery.do?sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L&STOCK_TYPE=1&COMPANY_STATUS=2,4,5,7,8&type=inParams&isPagination=true&pageHelp.pageSize=…`.
  - It **requires the header `Referer: https://www.sse.com.cn/`**; without it the response is "System Error".
  - Fields: `A_STOCK_CODE, SEC_NAME_CN, FULL_NAME_IN_ENGLISH, LIST_DATE, CSRC_CODE, AREA_NAME_DESC, DELIST_DATE`.
- **SZSE:** `https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1`. xlsx with 2,904 rows; columns include `板块, 公司全称, 英文名称, A股代码, A股上市日期, A股总股本, A股流通股本, 所属行业 (CSRC)`.
- **BSE (Beijing):** the direct endpoint redirects, so use AKShare `stock_info_bj_name_code`.
- **CNINFO:** `http://www.cninfo.com.cn/new/data/szse_stock.json` returns `code, orgId, zwjc`. `orgId` is required for the CNINFO announcement queries (`/new/hisAnnouncement/query` POST).
- **HKEX:** `https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx`. Use `header=2`; 17,205 rows, of which `Category=="Equity"` gives 2,826. Columns: `Stock Code, Name of Securities, Category, Sub-Category, Board Lot, ISIN, …`.

### 3.5 HKEXnews announcements and DI (disclosure of interests)
- **Announcements (JSON, no key, live-tested):**
  1. Map stock code to the internal id with `https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json` (`[{"i":7609,"c":"00700","n":"TENCENT"…}]`).
  2. Query `https://www1.hkexnews.hk/search/titleSearchServlet.do?sortDir=0&sortByOptions=DateTime&category=0&market=SEHK&stockId=7609&documentType=-1&fromDate=20260901&toDate=20261010&title=&searchType=0&t1code=-2&t2Gcode=-2&t2code=-2&rowRange=100&lang=E`.
  - The response is `{"result": "<JSON string>"}`, a JSON array encoded inside a string. Each item has `NEWS_ID, DATE_TIME ("09/10/2026 17:44" HKT), TITLE, LONG_TEXT, STOCK_CODE, FILE_TYPE, FILE_LINK (/listedco/listconews/sehk/2026/1009/2026100900649.pdf), TOTAL_COUNT`.
  - This is the point-in-time source for HK events.
- **DI** ([info](https://www2.hkexnews.hk/Shareholding-Disclosures/Disclosure-of-Interests?sc_lang=en)): there is no API, but the **HTML is parseable with `pd.read_html`** **(live-tested)**.
  1. `https://di.hkex.com.hk/di/NSSrchCorpList.aspx?sa1=cl&scsd=01/01/2026&sced=09/10/2026&sc=700&src=MAIN&lang=EN` returns the corporation id (`sid=6893`).
  2. `NSAllFormList.aspx?sa2=an&sid=6893&…&sd=…&ed=…` lists the forms with columns `Form Serial Number (e.g. DA20260819E00389: filing date embedded), Name of substantial shareholder / director / chief executive, Reason for disclosure (code e.g. 1316(L)), No. of shares bought/sold/involved, Average price per share, No. of shares interested, % of issued voting shares, Date of relevant event`.
  - Variants: `NSAllDirList` (directors only) and `NSAllSSList` (substantial shareholders).
  - DION filings start on 2017-07-03. Dates are dd/mm/yyyy.
  - Paid aggregators exist (e.g. Renavon) if a clean feed is needed.
- **Licensing:** HKEX website terms. Keep it low-rate, personal and not redistributed.

### 3.6 Geo and latency from Europe (Portugal), measured
- CNINFO, SSE, SZSE, Sina, Tencent, THS, HKEX and BaoStock all responded in 0.7–5 s.
- Eastmoney push2 was unreliable.
- **NBS `data.stats.gov.cn` returned 403 (`reason:UrlACL`) to our IPv6**, so use AKShare macro, IMF, BIS or OECD for China macro.
- Expect higher latency during Chinese evenings. Schedule China jobs at **01:00–06:00 Lisbon** (08:00–13:00 Beijing, after the close and daily database refreshes, still before most sites' daytime throttling).

---

## 4. Other Asia (viability notes)

| Market | Workable free path | Notes |
|---|---|---|
| **Taiwan** | **TWSE OpenAPI** `https://openapi.twse.com.tw/v1/…` (swagger, no key) **live-tested**: `exchangeReport/STOCK_DAY_ALL` (whole-market daily OHLCV; dates in ROC calendar, e.g. `1151008` = 2026-10-08) and `opendata/t187ap03_L` (company master: industry, listing date, capital). TPEx has a similar OpenAPI. MOPS (`mopsov.twse.com.tw`) holds financials and insider data as HTML forms | **Good.** Yahoo `.TW`/`.TWO` works |
| **India** | NSE archives work **without cookies** (live-tested): `https://archives.nseindia.com/content/equities/EQUITY_L.csv` (master with ISIN) and daily bhavcopy `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | `www.nseindia.com/api/*` (announcements, insider) needs a cookie session behind Akamai bot protection and is fragile. The BSE API redirects without a Referer. Fundamentals: no free structured feed (XBRL per filing only). Prices and master: **viable**. Fundamentals and insiders: **weak** |
| **Singapore** | SGX has no documented API (`api.sgx.com` exists but is undocumented and parameter-gated) | Yahoo `.SI` works. Low priority |
| **Australia** | ASX list CSV (live-tested): `https://www.asx.com.au/asx/research/ASXListedCompanies.csv` (GICS industry group) and the Markit endpoint `https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file` (+ listing date, market cap) | Announcements via the undocumented Markit API. Prices from Yahoo `.AX`. **Viable** for master and prices |

---

## 5. Global macro

### 5.1 FRED / ALFRED
- **Docs:** https://fred.stlouisfed.org/docs/api/fred/ . Key from https://fred.stlouisfed.org/docs/api/api_key.html (account at fredaccount.stlouisfed.org). Env var `FRED_APIKEY`, currently **empty in `.env`**.
- **Limit:** 120 requests/min per key (429 on excess). `limit` up to 100,000 observations per call.
- **Endpoints:**
  - `GET https://api.stlouisfed.org/fred/series/observations?series_id=DGS10&api_key=…&file_type=json&observation_start=…`
  - `fred/series?series_id=` (metadata: `last_updated, frequency, units, seasonal_adjustment`)
  - `fred/series/search?search_text=…&search_type=full_text`
  - `fred/series/vintagedates`
  - `fred/release/dates` (release calendar)
- **Point-in-time (ALFRED):** add `realtime_start`/`realtime_end`.
  - `realtime_start=1776-07-04&realtime_end=9999-12-31` returns **every vintage** of each observation, with `realtime_start/realtime_end` per row.
  - `output_type=2` with `vintage_dates=` returns a wide table by vintage (max 2,000 vintage dates per JSON call; 450 for daily series in CSV).
  - Store `(series_id, date, value, realtime_start, realtime_end)`. Only revised series need this (GDP, payrolls, CPI-SA, IP, retail sales); market series are not revised.
- **Keyless fallback:** `https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10[,ID2…]&cosd=YYYY-MM-DD` **(live-tested)** returns the latest vintage only, with column `observation_date`.
- **Python:** `fredapi` (pure Python, works on 3.13) or raw `requests`. Raw is preferred to control vintages.

### 5.2 Recommended FRED series (all verified alive on 2026-10-10; last observation in brackets)

**Growth / activity**

| ID | Series | Freq | Last obs |
|---|---|---|---|
| GDPC1 | Real GDP | Q | 2026-04 |
| A191RL1Q225SBEA | Real GDP growth, SAAR | Q | 2026-04 |
| GDPNOW | Atlanta Fed GDPNow | Q | 2026-07 |
| INDPRO | Industrial production | M | 2026-08 |
| TCU | Capacity utilization | M | 2026-08 |
| RRSFS | Real retail sales | M | 2026-08 |
| RSAFS | Retail sales | M | 2026-08 |
| PCEC96 | Real PCE | M | 2026-08 |
| W875RX1 | Real income ex transfers (recession dating) | M | 2026-08 |
| DGORDER | Durable goods orders | M | 2026-08 |
| ANDENO | Core capital goods orders (nondefense ex aircraft) | M | 2026-08 |
| CMRMTSPL | Real manufacturing and trade sales | M | 2026-07 |
| CFNAI | Chicago Fed National Activity Index | M | 2026-08 |
| CFNAIMA3 | CFNAI 3-month average | M | 2026-08 |
| WEI | Weekly Economic Index (Dallas Fed) | W | 2026-10-03 |
| GACDFSA066MSFRBPHI | Philly Fed manufacturing current activity (**ISM proxy**) | M | 2026-09 |
| BACTSAMFRBDAL | Dallas Fed manufacturing business activity (**ISM proxy**) | M | 2026-09 |
| RECPROUSM156N | Smoothed recession probability | M | 2026-08 |
| USREC | NBER recession indicator | M | 2026-09 |

**Inflation**

| ID | Series | Freq | Last obs |
|---|---|---|---|
| CPIAUCSL | CPI | M | 2026-08 |
| CPILFESL | Core CPI | M | 2026-08 |
| PCEPI | PCE price index | M | 2026-08 |
| PCEPILFE | Core PCE price index | M | 2026-08 |
| PPIFIS | PPI final demand | M | 2026-08 |
| CORESTICKM159SFRBATL | Atlanta Fed sticky-price core CPI | M | 2026-08 |
| MEDCPIM158SFRBCLE | Cleveland Fed median CPI | M | 2026-08 |
| MICH | Michigan inflation expectations | M | 2026-08 |
| T5YIE | 5-year breakeven | D | 2026-10-09 |
| T10YIE | 10-year breakeven | D | 2026-10-09 |
| T5YIFR | 5y5y forward inflation | D | 2026-10-09 |

**Labor**

| ID | Series | Freq | Last obs |
|---|---|---|---|
| PAYEMS | Nonfarm payrolls | M | 2026-09 |
| UNRATE | Unemployment rate | M | 2026-09 |
| U6RATE | U-6 underemployment | M | 2026-09 |
| ICSA | Initial claims | W | 2026-10-03 |
| CCSA | Continuing claims | W | 2026-10-03 |
| JTSJOL | JOLTS job openings | M | 2026-08 |
| CIVPART | Participation rate | M | 2026-09 |
| CES0500000003 | Average hourly earnings | M | 2026-09 |
| AWHMAN | Average weekly hours, manufacturing | M | 2026-09 |
| SAHMREALTIME | Real-time Sahm rule | M | 2026-09 |
| MANEMP | Manufacturing employment | M | 2026-09 |

**Rates and curve**

| ID | Series | Freq | Last obs |
|---|---|---|---|
| DFEDTARU | Fed funds target upper bound | D | 2026-10-10 |
| EFFR | Effective fed funds | D | 2026-10-08 |
| SOFR | SOFR | D | 2026-10-08 |
| DGS3MO | 3-month Treasury | D | 2026-10-08 |
| DGS2 | 2-year Treasury | D | 2026-10-08 |
| DGS5 | 5-year Treasury | D | 2026-10-08 |
| DGS10 | 10-year Treasury | D | 2026-10-08 |
| DGS30 | 30-year Treasury | D | 2026-10-08 |
| T10Y2Y | 10y–2y spread | D | 2026-10-09 |
| T10Y3M | 10y–3m spread | D | 2026-10-09 |
| T10YFF | 10y–fed funds spread | D | 2026-10-08 |
| DFII10 | 10-year TIPS real yield | D | 2026-10-08 |
| THREEFYTP10 | ACM 10-year term premium | D | 2026-10-02 |
| MORTGAGE30US | 30-year mortgage rate | W | 2026-10-08 |
| DPRIME | Prime rate | D | 2026-10-02 |

**Credit**

| ID | Series | Freq | Last obs | Note |
|---|---|---|---|---|
| BAMLH0A0HYM2 | US high-yield OAS | D | 2026-10-08 | **3-year history only** |
| BAMLH0A3HYC | CCC & lower OAS | D | 2026-10-08 | **3-year history only** |
| BAMLC0A0CM | US investment-grade OAS | D | 2026-10-08 | **3-year history only** |
| BAMLC0A4CBBB | BBB OAS | D | 2026-10-08 | **3-year history only** |
| BAMLEMCBPIOAS | EM corporate OAS | D | 2026-10-08 | **3-year history only** |
| BAMLHE00EHYIOAS | Euro high-yield OAS | D | 2026-10-08 | **3-year history only** |
| AAA10Y | Moody's Aaa minus 10y | D | 2026-10-08 | Full history from 1983/86: **use for long backtests** |
| BAA10Y | Moody's Baa minus 10y | D | 2026-10-08 | Full history from 1983/86: **use for long backtests** |
| DRTSCILM | SLOOS lending standards | Q | 2026-07 | |

**Financial conditions and volatility**

| ID | Series | Freq | Last obs |
|---|---|---|---|
| NFCI | Chicago Fed financial conditions | W | 2026-10-02 |
| ANFCI | Adjusted NFCI | W | 2026-10-02 |
| STLFSI4 | St. Louis Fed financial stress | W | 2026-10-02 |
| KCFSI | Kansas City Fed financial stress | M | 2026-09 |
| VIXCLS | VIX | D | 2026-10-08 |
| OVXCLS | Oil volatility | D | 2026-10-08 |
| GVZCLS | Gold volatility (**gold price proxy, see dead list**) | D | 2026-10-08 |

**USD and FX (H.10, published weekly on Mondays, about a 1-week lag)**

| ID | Series | Freq | Last obs |
|---|---|---|---|
| DTWEXBGS | Broad dollar index | D | 2026-10-02 |
| DTWEXAFEGS | Dollar vs advanced foreign economies | D | 2026-10-02 |
| DTWEXEMEGS | Dollar vs emerging markets | D | 2026-10-02 |
| DEXJPUS | JPY per USD | D | 2026-10-02 |
| DEXKOUS | KRW per USD | D | 2026-10-02 |
| DEXCHUS | CNY per USD | D | 2026-10-02 |
| DEXHKUS | HKD per USD | D | 2026-10-02 |
| DEXTAUS | TWD per USD | D | 2026-10-02 |
| DEXINUS | INR per USD | D | 2026-10-02 |
| DEXBZUS | BRL per USD | D | 2026-10-02 |
| DEXMXUS | MXN per USD | D | 2026-10-02 |
| DEXUSUK | USD per GBP | D | 2026-10-02 |
| DEXUSEU | USD per EUR | D | 2026-10-02 |

**Commodities**

| ID | Series | Freq | Last obs |
|---|---|---|---|
| DCOILWTICO | WTI crude | D | 2026-10-06 |
| DCOILBRENTEU | Brent crude | D | 2026-10-06 |
| DHHNGSP | Henry Hub natural gas | D | 2026-10-06 |
| PCOPPUSDM | Copper (IMF) | M | 2026-07 |
| PIORECRUSDM | Iron ore (IMF) | M | 2026-07 |
| PWHEAMTUSDM | Wheat (IMF) | M | 2026-07 |
| PALLFNFINDEXM | All-commodity index (IMF) | M | 2026-07 |

**Housing**

| ID | Series | Freq | Last obs |
|---|---|---|---|
| HOUST | Housing starts | M | 2026-08 |
| PERMIT | Building permits | M | 2026-08 |
| CSUSHPISA | Case-Shiller national | M | 2026-07 |
| MSPUS | Median sale price | Q | 2026-04 |

**Sentiment and liquidity**

| ID | Series | Freq | Last obs |
|---|---|---|---|
| UMCSENT | Michigan consumer sentiment | M | 2026-08 |
| M2SL | M2 | M | 2026-08 |
| WALCL | Fed balance sheet | W | 2026-10-07 |
| RRPONTSYD | Overnight reverse repo | D | 2026-10-09 |
| WRESBAL | Reserve balances | W | 2026-10-07 |
| BUSLOANS | C&I loans | M | 2026-09 |
| TOTALSL | Consumer credit | M | 2026-08 |

**International on FRED (alive)**

| IDs | Series | Last obs |
|---|---|---|
| USALOLITOAASTSAM, JPNLOLITOAASTSAM, KORLOLITOAASTSAM, CHNLOLITOAASTSAM, DEULOLITOAASTSAM, GBRLOLITOAASTSAM, BRALOLITOAASTSAM, MEXLOLITOAASTSAM, G7LOLITOAASTSAM | OECD composite leading indicators (**PMI proxy**) | 2026-08 |
| ECBDFR | ECB deposit rate | 2026-10-09 |
| IRSTCI01JPM156N, IRSTCI01KRM156N, IRSTCI01GBM156N, IRSTCI01MXM156N, IRSTCI01BRM156N | Call money rates | 2026-08 |
| IRLTLT01JPM156N, IRLTLT01KRM156N, IRLTLT01DEM156N, IRLTLT01GBM156N, IRLTLT01MXM156N | 10-year government yields | 2026-08 |
| INTGSTBRM193N | Brazil government bond yield | 2026-08 |
| INTDSRBRM193N | Brazil discount rate | 2026-08 |
| CP0000EZ19M086NEST | Euro-area HICP | 2026-08 |
| LRHUTTTTJPM156S, LRHUTTTTKRM156S, LRHUTTTTGBM156S, LRHUTTTTMXM156S | Unemployment rates | 2026-05 to 2026-07 |
| CLVMNACSCAB1GQEA19 | Euro-area real GDP | 2026-Q2 |
| NAEXKP01JPQ657S, NAEXKP01KRQ657S | Japan and Korea GDP growth | 2026-Q2 |

### 5.3 Dead or stale FRED IDs (do NOT use)

| ID | Status |
|---|---|
| `NAPM`, `NAPMNOI`, `ISM` (ISM PMI) | **404, removed** (ISM licensing, 2016). Use CLI, Philly, Dallas and CFNAI |
| `GOLDAMGBD228NLBM` (LBMA gold) | **404, removed** (2022) |
| `TEDRATE` | Ends 2022-01 (LIBOR) |
| `USSLIND` | Ends 2020-02 |
| `OECDLOLITOAASTSAM` | Ends 2022-11 (use G7 or country CLIs) |
| `IRSTCB01CNM156N` | Ends 2023-11 (use BIS WS_CBPOL for China LPR) |
| `CPALTT01{JP,KR,CN,GB,BR,MX}M659N`, `*CPIALLMINMEI` | **Stale (2021–2025)**. OECD MEI series are no longer refreshed; use IMF CPI, ECOS, BCB or national sources |
| `LRHUTTTTEZM156S`, `LRHU24TTEZM156S` | Ends 2023-01 (use Eurostat `une_rt_m`) |
| `JPNPROINDMISMEI`, `KORPROINDMISMEI` | Ends 2024-03. `CHNPROINDMISMEI` is 404 |
| `IRLTLT01BRM156N`, `GACDINA066MSFRBNY`, `WILL5000PR` | 404 |
| `INTDSRJPM193N` | Ends 2017. `BSCICP03USM665S`, `CSCICP03USM665S` and `CHNGDPNQDSMEI` are stale |
| `IR3TIB01EZM156N`, `IRLTLT01EZM156N` | Last obs 2026-01 (lagging). Prefer the ECB |

**Licensing-truncated series:**
- ICE BofA OAS: **3 years only** since Apr-2026. Start the local archive now, because history before 2023-10 is gone from FRED.
- `SP500` and `DJIA`: 10 years.
- `EXHOSLUSM495S`: 1 year.

### 5.4 Direct national and multilateral APIs (all keyless unless noted; **live-tested**)

| Source | Base / example | Notes |
|---|---|---|
| **ECB Data Portal** | `https://data-api.ecb.europa.eu/service/data/{flow}/{key}?format=csvdata&lastNObservations=N` | No key. Keys verified: `FM/D.U2.EUR.4F.KR.DFR.LEV` (deposit rate), `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y` (EA AAA 10y), **`HICP/M.U2.N.000000.4D0.ANR`** (new dataset; **old `ICP` stops at 2025-12**), `MNA/Q.Y.I9.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.GY` (GDP y/y), `EXR/D.{CUR}.EUR.SP00.A` (FX) |
| **Eurostat** | `https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}?geo=EA&…&format=JSON` (JSON-stat 2.0) | **`prc_hicp_minr`** (`coicop18=TOTAL`, `unit=RCH_A`). **`prc_hicp_manr` is archived (ends 2025-12)**. Also `une_rt_m`, `namq_10_gdp`, `sts_inpr_m`, `ei_bssi_m_r2` (ESI, sentiment proxy). Catalogue: `/catalogue/toc/txt?lang=en` |
| **Bank of England IADB** | `https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01/Sep/2026&Dateto=now&SeriesCodes=IUDBEDR&CSVF=TN&UsingCodes=Y` | Bank rate `IUDBEDR`. Send a browser UA |
| **ONS** | `https://api.beta.ons.gov.uk/v1/datasets` | CPIH and labour market |
| **Bank of Japan, new API (launched 2026-02-18)** | `https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=json&lang=en&db=FM01&code=STRDCLUCON&startDate=202609` | **No key.** Also `/getDataLayer`, `/getMetadata`. Limits: ≤250 series and ≤60,000 data points per request; `NEXTPOSITION` paging. "Excessive access may be restricted". [Manual](https://www.stat-search.boj.or.jp/info/api_manual_en.pdf) |
| **BoK ECOS** | `https://ecos.bok.or.kr/api/StatisticSearch/{KEY}/json/en/{start}/{end}/{STAT}/{FREQ}/{from}/{to}/{ITEM}` | **Key** (free, [apply](https://ecos.bok.or.kr/api/#/AuthKeyApply), env `BOK_ECOS_APIKEY`). The `sample` key works for ≤10 rows. Verified: `722Y001/M/…/0101000` (base rate), `817Y002/D/…/010210000` (10y KTB), `901Y009/M/…/0` (CPI index) |
| **BCB SGS (Brazil)** | `https://api.bcb.gov.br/dados/serie/bcdata.sgs.{id}/dados?formato=json&dataInicial=dd/mm/yyyy&dataFinal=…` | **Daily series need ≤10-year windows** (error otherwise). `432` Selic target (**contains future-dated rows up to the next Copom meeting**, so filter `date<=today`). `433` IPCA m/m, `13522` IPCA 12m, `24363` IBC-Br, `1` USD/BRL. PTAX via Olinda OData |
| **Banxico SIE** | `https://www.banxico.org.mx/SieAPIRest/service/v1/series/{ids}/datos/{yyyy-mm-dd}/{yyyy-mm-dd}` with header `Bmx-Token` | **Token** (free, [get](https://www.banxico.org.mx/SieAPIRest/service/v1/token), env `BANXICO_TOKEN`). Limits: 200 queries per 5 min and 10,000/day (historical); 80/min and 40,000/day (`oportuno`). Series: `SF61745` target rate, `SF43718` FIX, `SF60648` TIIE 28d; INPC `SP1` (verify) |
| **BIS** | `https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/M.JP+KR+CN+BR+MX+GB+XM+US?startPeriod=2026-06&format=csv` | **Best single source for policy rates** (current to 2026-09). Also `WS_LONG_CPI`, `WS_XRU`, `WS_TC` (credit) |
| **IMF SDMX (new)** | `https://api.imf.org/external/sdmx/2.1/data/IMF.STA,CPI/JPN+KOR.CPI._T.YOY_PCH_PA_PT.M?startPeriod=2026-01` (SDMX 2.1 and 3.0) | **The old `dataservices.imf.org` is gone (DNS fails). IFS is retired**, replaced by topic dataflows (CPI, MFS, …). CSV includes long multi-line text attributes, so request SDMX-JSON or post-filter columns |
| **OECD SDMX** | `https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.1/USA+JPN.M.LI...AA...H?startPeriod=2025-01&format=csvfilewithlabels` | **Limit: 60 data downloads/hour; VPN traffic blocked.** Cache aggressively |
| **World Bank** | `https://api.worldbank.org/v2/country/JP;KR/indicator/NY.GDP.MKTP.KD.ZG?format=json&date=2020:2025` | Annual. Fine for structural data |
| **DBnomics** | `https://api.db.nomics.world/v22/series/{provider}/{dataset}/{series}?observations=1` | One API over about 90 providers, but **check `indexed_at`**: IMF/IFS last indexed 2025-08, NBS 2026-01, and BIS WS_CBPOL **stuck at 2025-06**. Use only as a discovery or fallback tool |
| **China NBS** | `data.stats.gov.cn` | **403 from the EU.** Use AKShare `macro_china_*` (CPI, PPI, PMI, IP), IMF CPI, BIS policy rate and the FRED/OECD CLI |

### 5.5 Minimal macro set per country (policy rate, CPI, GDP/IP, unemployment, 10Y, PMI proxy)

| Country | Policy rate | CPI | GDP / IP | Unemployment | 10Y | PMI proxy / CLI |
|---|---|---|---|---|---|---|
| US | FRED DFEDTARU | FRED CPIAUCSL, CPILFESL | GDPC1, INDPRO | UNRATE | DGS10 | CFNAI, Philly, Dallas, USALOLITOAASTSAM |
| Euro Area | ECB `FM…DFR` | ECB `HICP…ANR` / Eurostat `prc_hicp_minr` | ECB MNA / Eurostat `namq_10_gdp`, `sts_inpr_m` | Eurostat `une_rt_m` | ECB `YC…SR_10Y` | Eurostat ESI `ei_bssi_m_r2`, DEU CLI |
| UK | BoE `IUDBEDR` | ONS (CPIH) / IMF CPI | ONS / OECD | FRED LRHUTTTTGBM156S | FRED IRLTLT01GBM156N | GBR CLI |
| Japan | BoJ `FM01 STRDCLUCON` / BIS | IMF CPI (JPN) | FRED NAEXKP01JPQ657S; BoJ Tankan via API | FRED LRHUTTTTJPM156S | FRED IRLTLT01JPM156N | JPN CLI, BoJ Tankan DI |
| Korea | ECOS `722Y001` | ECOS `901Y009` | FRED NAEXKP01KRQ657S / ECOS | FRED LRHUTTTTKRM156S | ECOS `817Y002` | KOR CLI |
| China | BIS WS_CBPOL (LPR) | AKShare `macro_china_cpi_yearly` / IMF CPI | AKShare `macro_china_gdp`, industrial production | AKShare urban unemployment | AKShare/BIS (CGB 10y) | AKShare `macro_china_pmi_yearly` (official PMI), CHN CLI |
| Brazil | BCB SGS 432 | SGS 433/13522 | SGS 24363 (IBC-Br) | IMF/IBGE | FRED INTGSTBRM193N (monthly) | BRA CLI |
| Mexico | Banxico SF61745 | Banxico/INEGI | FRED / INEGI (token) | FRED LRHUTTTTMXM156S | FRED IRLTLT01MXM156N | MEX CLI |

**Point-in-time for non-US macro:** none of these APIs serve vintages (ALFRED is US-centric). Store a **nightly snapshot with `knowledge_date = retrieval date`** and, for backfill, apply conservative publication lags (CPI +15 to 20 days, GDP +30 to 60 days, CLI +40 days).

---

## 6. FX: single source of truth
- **Primary: ECB euro reference rates.** `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip` (0.64 MB, from 1999-01-04, about 16:00 CET each TARGET day) **(live-tested)**.
  - It covers USD, JPY, GBP, CNY, HKD, KRW, INR, BRL, MXN, plus CZK, DKK, HUF, PLN, RON, SEK, CHF, ISK, NOK, TRY, AUD, CAD, IDR, ILS, MYR, NZD, PHP, SGD, THB, ZAR. Cross via EUR.
  - The incremental daily file is `eurofxref-daily.xml`, or use the API `EXR/D.{CUR}.EUR.SP00.A`. No key; open use.
- **Gaps (not in the ECB set):**

| Currency | Source | Status |
|---|---|---|
| TWD | FRED `DEXTAUS` (H.10, about 1-week lag) | Verified |
| CLP | `mindicador.cl/api/dolar` (dólar observado, no key) | Verified. Official BCCh SieteRestWS needs user/pass |
| ARS | BCRA `https://api.bcra.gob.ar/estadisticascambiarias/v1.0/Cotizaciones/USD?fechadesde=…&fechahasta=…` (no key) | Verified |
| COP | `https://www.datos.gov.co/resource/32sa-8pi3.json` (TRM) | Verified |
| PEN | BCRP `https://estadisticas.bcrp.gob.pe/estadisticas/series/api/PD04640PD/json/{from}/{to}` | Verified |
| BRL (alternative) | BCB PTAX (Olinda) | Verified |

- **Cross-check:** Yahoo `{CCY}=X` returned all 14 required pairs. Use it for anomaly checks only (unofficial).
- **Convention:** store everything as **units per USD at the source fixing time**, with the source and fixing timestamp. Never mix ECB 16:00 CET with Yahoo close in one series.

---

## 7. GDELT (events, tone and news volume)

### 7.1 DOC 2.0 API
- **Base:** `https://api.gdeltproject.org/api/v2/doc/doc`. [Docs](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/). No key.
- **Query operators:** `"exact phrase"`, `(a OR b)`, `-term`, `domain:`, `domainis:`, `sourcecountry:`, `sourcelang:`, `theme:`, `tone<` / `tone>` / `toneabs>`, `near20:"a b"`, `repeat3:"x"`, `imagetag:`.
- **Keywords shorter than 3 characters are rejected** **(live-tested: `query=AI` returned "Your search contained a keyword that was too short.")**.
- **Modes:** `ArtList`, `TimelineVol`, `TimelineVolRaw`, `TimelineVolInfo`, `TimelineTone`, `TimelineLang`, `TimelineSourceCountry`, `ToneChart`, image modes.
- **Formats:** `json`, `csv`, `rss`, `jsonfeed`, `html`.
- `maxrecords` is up to **250** (default 75). Sort: `DateDesc`, `DateAsc`, `ToneDesc`, `ToneAsc`, `HybridRel`.
- **Window:** the documented window is a rolling **3 months** (`timespan=15min…3m`, or `startdatetime`/`enddatetime` as `YYYYMMDDHHMMSS`). `timelinesmooth` is ≤30.
  - **(live-tested)** An explicit `startdatetime=20260601000000&enddatetime=20260602000000` (131 days back) still returned June-2026 articles, so older windows *may* work.
  - Do not build on that: use raw files or BigQuery for history.
- **Rate limit (live-tested):** the server replies `429 "Please limit requests to one every 5 seconds…"`.
  - We hit 429s even at 30–90 s spacing, probably because of other concurrent agents on the same IP. Treat the effective budget as **≤1 request per 6–10 s from a single worker**, with exponential backoff.
  - **Latency was 9–20 s per call.**
- **Actual responses (live, 2026-10-10):**
  - `timelinetone` (Nvidia, 7d): `{"query_details":{"title":"Nvidia","date_resolution":"hour"},"timeline":[{"series":"Average Tone","data":[{"date":"20261006T080000Z","value":0.0433}, …]}]}`
  - `timelinevolraw` (Nvidia, 1m): `{"timeline":[{"series":"Article Count","data":[{"date":"20260911T000000Z","value":381,"norm":154253}, …]}]}`. `value` is the matching articles and `norm` is the total articles monitored that day.
  - `artlist`: `{"articles":[{"url","url_mobile","title","seendate":"20261010T064500Z","socialimage","domain","language":"English","sourcecountry":"Singapore"}]}`. **There is no tone per article in artlist** (get it from GKG).
  - `tonechart`: `{"tonechart":[{"bin":-10,"count":1,"toparts":[{"url","title"}]}, …]}`.
- **Warning: coverage collapse observed.** The `norm` field fell from about 100k–175k articles/day (Sep-2026) to **3,958 on Oct-06, 678 on Oct-08 and 1,836 on Oct-10**. Whole days were missing (Sep 14–19 and Oct 3–5, 7, 9).
  - Raw GKG files for Oct-08 were present (505 MB zipped), so the DOC index is lagging or degraded rather than GDELT being down.
  - **Build a health check:** reject or flag days where `norm` is below 50% of its 30-day median, and never interpret the volume drop as "less news".
- Syndicated duplicates (the same story on dozens of Big News Network domains) are common, so **dedupe on normalized title**.

### 7.2 Raw files (preferred bulk path)
- **Index:** `http://data.gdeltproject.org/gdeltv2/lastupdate.txt` (and `lastupdate-translation.txt`). Each line holds `size md5 url`.
  - The full history list is `masterfilelist.txt` (and `-translation`), which is very large; fetch it once, then follow `lastupdate`.
  - HTTP requests **301-redirect to `https://data.gdeltproject.org:443/…`**, so use https directly (HEAD requests do not auto-follow in `requests`).
- **Measured volume for 2026-10-08** (96 slots each, all present):

| Stream | Zipped per day |
|---|---|
| `export.CSV.zip` (Events) | 7.8 MB |
| `mentions.CSV.zip` | 11.3 MB |
| `gkg.csv.zip` (English GKG) | **504.8 MB** |
| `translation.export` | 5.7 MB |
| `translation.mentions` | 7.8 MB |
| `translation.gkg` | **889.6 MB** |
| **Total** | **≈1.43 GB/day zipped, ≈4.5–5.5 GB raw** |

- **Measured processing:** 1 hour (4 slots, English + translingual GKG) was 75.7 MB zipped, 240 MB raw and 18,969 records. **Download took 10.3 s, and parsing plus org-matching took 3.8 s single-threaded.**
  - A full day is therefore about 4 min download and about 90 s parse on one core, or about 10–15 s with 12 processes.
  - Keep only matched rows and daily aggregates (≈10–50 MB/day).
- **GKG 2.1 columns** (27, tab-separated, `quoting=csv.QUOTE_NONE`, `csv.field_size_limit` raised):
  `GKGRECORDID, DATE (YYYYMMDDHHMMSS), SourceCollectionIdentifier, SourceCommonName, DocumentIdentifier (URL), V1Counts, V2.1Counts, V1Themes, **V2EnhancedThemes** (THEME,charoffset;…), V1Locations, **V2EnhancedLocations**, V1Persons, V2EnhancedPersons, V1Organizations, **V2EnhancedOrganizations** (Name,offset;…), **V1.5Tone** (Tone, PositiveScore, NegativeScore, Polarity, ActivityRefDensity, SelfGroupRefDensity, WordCount), V2.1EnhancedDates, V2GCAM, V2.1SharingImage, V2.1RelatedImages, V2.1SocialImageEmbeds, V2.1SocialVideoEmbeds, V2.1Quotations, V2.1AllNames, V2.1Amounts, V2.1TranslationInfo (srclc:xxx;eng:…), V2ExtrasXML (<PAGE_TITLE>, <PAGE_AUTHORS>)`.
- **Events 2.0 (`export`, 61 columns):**
  - `GLOBALEVENTID, SQLDATE, MonthYear, Year, FractionDate`
  - `Actor1Code, Actor1Name, Actor1CountryCode, Actor1KnownGroupCode, Actor1EthnicCode, Actor1Religion1Code, Actor1Religion2Code, Actor1Type1Code…Type3Code`, then the same for Actor2
  - `IsRootEvent, EventCode, EventBaseCode, **EventRootCode** (CAMEO 01–20), **QuadClass** (1–4), **GoldsteinScale** (−10…+10), **NumMentions**, NumSources, NumArticles, **AvgTone**`
  - `Actor1Geo_*`, `Actor2Geo_*`, `ActionGeo_Type, ActionGeo_FullName, ActionGeo_CountryCode (FIPS), ADM1, ADM2, Lat, Long, FeatureID`
  - `DATEADDED, SOURCEURL`
- **Mentions (16 columns):** `GLOBALEVENTID, EventTimeDate, MentionTimeDate, MentionType, MentionSourceName, MentionIdentifier, SentenceID, Actor1CharOffset, Actor2CharOffset, ActionCharOffset, InRawText, Confidence, MentionDocLen, MentionDocTone, MentionDocTranslationInfo, Extras`.
- **CAMEO roots for an equity/macro risk index:** 14 Protest, 17 Coerce, 18 Assault, 19 Fight, 20 Mass violence (conflict); 03, 04, 05 and 06 (cooperation); 16 Reduce relations (sanctions).
- **Point-in-time:** GKG `DATE` / Events `DATEADDED` (UTC, 15-min). They are available about 15 min after the slot.
- **Global Entity Graph (GEG):** `gdeltv3/geg_gcnlapi` holds Google Cloud NL annotations (entities with salience and Knowledge Graph MIDs).
  - The main corpus is a **sample from 2016-07 to 2019-10**; later updates are partial. The HTTP directory index currently returns "placeholder".
  - Useful only for historical entity-linking research, not the nightly pipeline.
- **BigQuery option:** `gdelt-bq.gdeltv2.gkg_partitioned`, `events_partitioned`, `geg_gcnlapi`.
  - On-demand pricing is about **$6.25/TB scanned after the 1 TB/month free tier**. Filter `_PARTITIONTIME`; one day of GKG is roughly 15 GB scanned for wide selects.
  - It is good for one-off historical backfills (e.g. 2018–2026 tone per company). It is not needed nightly; local raw files are free.
- **Licensing:** GDELT is open for any use with citation.

### 7.3 Recommended cheap daily GDELT strategy

| Step | What | Volume / time on a 20-thread, 32 GB box |
|---|---|---|
| A. Raw GKG scan (primary) | Download the 192 GKG files (EN + translingual) for D-1 with `asyncio`/`httpx` (8 concurrent). Stream-parse in a `ProcessPool` (12 workers). Match `V2EnhancedOrganizations` (and `AllNames`) against an alias dictionary (company legal name, short name, ticker-in-parentheses patterns, local-script names from EDINET, DART and CNINFO masters), using **Aho–Corasick** (`pyahocorasick`, wheels for 3.13). Emit per (company, day): article count, mean/median V1.5Tone, positive/negative scores, top themes (ECON_*, WB_*), source countries, and URLs of the top-N articles | ≈1.4 GB download (≈3–5 min at 50–100 Mbit/s), ≈1–2 min CPU with 12 processes, <4 GB RAM |
| B. Events aggregation | Download 96 + 96 export files (13.5 MB). Aggregate by `ActionGeo_CountryCode` × `EventRootCode` × day: count, sum(NumMentions), mean(GoldsteinScale), mean(AvgTone). Map countries to sector exposure tables for a "geopolitical stress" factor | <30 s |
| C. DOC API (narrow) | Only for the **top ~100–200 watchlist names** and only `timelinevolraw` + `timelinetone` with `timespan=1d` (or weekly 7d). Run sequentially with ≥6 s spacing and backoff. Use `norm` as a coverage guard | 200–400 calls × (6 s + 10–20 s latency) ≈ **1.5–2.5 h**, so run as a low-priority background job |
| D. Headline sentiment | Feed `<PAGE_TITLE>` from GKG `Extras` (plus DOC artlist titles) to the NLP models (§8) | ≈5–20k titles/day, ≈2–8 min CPU |
| E. Backfill | One-off: raw files from 2015-02 (GDELT 2.0 start) for the needed years (≈0.5–1.5 TB zipped total; do it per year), or BigQuery for selected entities | Hours to days, run once |

---

## 8. News and sentiment NLP (CPU only)

**Benchmark on this box (2026-10-10):** torch 2.14.1+cpu, transformers 5.19.0, 16 threads. Other agents were using the CPU at the same time (load 10–27%), so read these as **lower bounds**.

| Model | Languages | Params / RAM | Throughput, mixed 12-language set (batch 32, len ≤64) | Throughput, short EN headlines (batch 64) | Quality on our 12 test headlines |
|---|---|---|---|---|---|
| `ProsusAI/finbert` | EN | 110M / +520 MB | 26/s | **31/s (1 thread), 60/s (4), 79/s (8), 76/s (16)** | EN 5/5 correct-ish (labels "steady growth, shares unchanged" positive). **All non-EN are neutral (useless)** |
| `ProsusAI/finbert`, int8 dynamic quantization | EN | 488 MB | 35/s (+35%) | – | `torch.ao.quantization` is **deprecated** (use torchao or ONNX Runtime int8) |
| `yiyanghkust/finbert-tone` | EN | 110M | – | – | **Fails with `AutoTokenizer`/`AutoModel` on transformers 5.x** (config has no `model_type`). Works with `BertTokenizer` + `BertForSequenceClassification` (labels 0 Neutral, 1 Positive, 2 Negative) |
| `mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis` | EN | 82M / +135 MB | 29/s | ≈1.5–2× FinBERT on idle CPU | EN good (Apache-2.0) |
| `cardiffnlp/twitter-xlm-roberta-base-sentiment` | ~100 langs | 278M / **+1.0 GB** | 46/s | – | **Best multilingual:** correct on JA (Toyota cut), ES (Santander), FR (LVMH); KO/ZH/DE/PT neutral (conservative) |
| `lxyuan/distilbert-base-multilingual-cased-sentiments-student` | 12 langs | 135M / +570 MB | 78/s | – | **Unusable:** almost everything positive, Nvidia surge negative |
| `tabularisai/multilingual-sentiment-analysis` | 23 langs | 135M | not run | – | CC-BY-**NC**-4.0 |
| `Kenpache/multilingual-financial-sentiment-v2` | 10 langs incl. JA/KO/ZH/PT | 560M (XLM-R large) | not run (≈4× slower than base) | – | New (May-2026), low adoption. Worth an offline evaluation |
| Local specialists | – | – | not run | – | `snunlp/KR-FinBert-SC` (KO), `IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment` (ZH), `lucas-leme/FinBERT-PT-BR` (PT) |

**Recommendation:**
1. Run language detection first: GDELT `TranslationInfo` gives `srclc` for free; otherwise use `fasttext-langdetect` or `lingua`.
2. **EN goes to FinBERT** (or the distilroberta model for speed). Everything else goes to **XLM-R cardiffnlp**, with optional per-language specialists for KO, ZH and PT once validated.
3. Translingual GKG rows already carry GDELT machine-translated tone (`V1.5Tone`), so use that as a free second opinion.
4. For speed, export to **ONNX Runtime with int8** (expect about 2–3× faster) and run 3–4 processes × 4 threads rather than one 16-thread process (it scales poorly beyond 8 threads).
5. Budget: 20k headlines/day ≈ 4–8 min. RAM ≈2 GB for both models.
6. On Windows set `HF_HUB_DISABLE_SYMLINKS_WARNING=1` (or enable Developer Mode) and `PYTHONUTF8=1`.

**Other free per-company news feeds:**
- **Google News RSS** works **(live-tested)**: `https://news.google.com/rss/search?q=%22Toyota%20Motor%22%20when:7d&hl=en-US&gl=US&ceid=US:en` returned 72 items, and the Japanese edition (`hl=ja&gl=JP&ceid=JP:ja`) returned 104.
  - Each item has `title (incl. " - Source")`, `pubDate (GMT)` and `source`. **Links are opaque `news.google.com/rss/articles/CBMi…` redirects.**
  - Google's ToS discourage automated use. Keep it to ≤1 query per 5–10 s, use it as a low-volume complement, and expect 429/CAPTCHA if abused.
- **Yahoo Finance RSS** (`feeds.finance.yahoo.com/rss/2.0/headline?s=…`) returned **generic market news, not company-specific**, so it is not useful.
- **Exchange and regulator primary sources** (best signal, all point-in-time): TDnet (JP), DART `list.json` (KR), CNINFO announcements (CN), HKEXnews titleSearch (HK), EDINET (JP). Their titles can be scored by the same NLP stack. *Polygon news is covered by another agent.*

---

## 9. Recommended ingestion strategy

### 9.1 Primary and fallback per region and data class

| Region | Security master | Prices (daily) | Fundamentals | Insiders / holders | Filings text / events | Macro |
|---|---|---|---|---|---|---|
| **Japan** | JPX `data_j.xlsx` (monthly) + EDINET code list (daily) | Yahoo `.T` (fallback: J-Quants Light) | EDINET `type=5` CSV (annual + half-year). Q1/Q3 from J-Quants Light (optional, paid) or TDnet tanshin XBRL | EDINET 350/360 (large holding). JP has no Form-4 equivalent for directors (short-swing reports are not in EDINET) | TDnet list (daily, 31-day retention) + EDINET docs | BoJ API, FRED/OECD, IMF CPI |
| **Korea** | DART `corpCode.xml` + KIND list | **KRX Open API** (fallback: Naver, Yahoo) | DART `fnlttSinglAcntAll` (fallback `fnlttMultiAcnt`) | DART `elestock`, `majorstock`, `hyslrSttus` | DART `list.json` + `document.xml` | ECOS, BIS |
| **China A** | SSE (Referer) + SZSE xlsx + BSE via AKShare + CNINFO orgId | **BaoStock** (fallback: AKShare Sina/Tencent) | BaoStock (PIT `pubDate`) + AKShare Sina statements. Eastmoney by-report incrementally (`NOTICE_DATE`) | AKShare CNINFO share changes. Eastmoney executive holdings (flaky; weekly, timeout-guarded) | CNINFO announcements (AKShare) | AKShare macro, BIS, IMF, OECD CLI |
| **Hong Kong** | HKEX ListOfSecurities.xlsx + HKEXnews stock-id JSON | Yahoo `.HK` / AKShare Sina `stock_hk_daily` | AKShare `stock_financial_hk_report_em` + indicators | **HKEX DI HTML** | **HKEXnews titleSearch JSON** | (HKMA; mostly USD peg, low priority) |
| Taiwan / India / AU (bonus) | TWSE OpenAPI / NSE EQUITY_L / ASX CSV | TWSE STOCK_DAY_ALL / NSE bhavcopy / Yahoo | MOPS (TW) / none (IN) / none (AU) | MOPS / – / – | – | FRED, OECD |
| Global macro | – | – | – | – | – | **FRED/ALFRED** + ECB + Eurostat + BoE + BoJ + ECOS + BCB + Banxico + BIS + IMF + OECD |
| FX | – | **ECB ref rates** + FRED DEXTAUS + BCRA, BCRP, TRM, mindicador | – | – | – | – |
| Events | – | – | – | – | **GDELT raw GKG + Events** (DOC API for the watchlist) + Google News RSS (light) | – |

### 9.2 Nightly request budget and runtime (incremental, steady state)

| Job | Requests/night | Runtime | Window (Lisbon) |
|---|---|---|---|
| EDINET list (D-1 plus re-poll D-2…D-7) + doc downloads | 7 + 50–600 (peak ~2,500/day in late June) | 2–15 min (peak ~45 min) at 1 req/s | 23:00 |
| TDnet pages + XBRL zips | 3–10 + 20–700 (earnings peaks) | 1–15 min | 23:00 |
| JPX list (1st business day of month) | 1 | <10 s | – |
| OpenDART list (D-1, all types, 100/page) | 5–20 | <1 min | 23:30 |
| OpenDART financials (only companies that filed A-type reports; ×CFS/OFS) | 0–5,000 (quarter peaks) | ≤30 min at 3 req/s (cap 20k/day) | 23:30 |
| OpenDART `elestock`/`majorstock` (companies with D-type filings that day) | 20–200 | 1–2 min | 23:30 |
| KRX Open API (KOSPI + KOSDAQ trades + base info) | 4 | <30 s | 01:00 (after the 08:00 KST update means ≥00:00 Lisbon) |
| BaoStock prices (≈5,500 A-shares, sequential) | 5,500 | 15–30 min | 02:00 |
| AKShare CN statements for companies that announced (from CNINFO list) | 50–1,500 | 5 min to 2 h (Sina 4 s/stock; quarter peaks run over 2–3 nights) | 02:30 |
| CNINFO announcements (date-window queries) | 10–50 | 2 min | 02:00 |
| HKEXnews titleSearch (watchlist or all-market by date) | 20–300 | 5 min | 02:00 |
| HKEX DI (watchlist, weekly full) | 100–2,600 (weekly) | 10 min to 1.5 h | Sat |
| HK prices (Yahoo or Sina, watchlist) | 200–2,800 | 5–30 min | 02:00 |
| FRED (≈150 series, ALFRED only for revised series) | ≈150–200 | 2–3 min (≤2 req/s) | 22:00 |
| ECB, Eurostat, BoE, BoJ, ECOS, BCB, Banxico, BIS, IMF, OECD (OECD ≤60/h) | ≈60–100 | 5 min | 22:00 |
| ECB FX + gap-currency APIs | ≈8 | <30 s | 17:00 CET+ |
| GDELT raw (GKG + Events + Mentions D-1) | ≈480 files, 1.4 GB | 5–8 min | 01:00 UTC+ |
| GDELT DOC API (watchlist 150 × 2 modes) | ≈300 | 1.5–2.5 h background | 03:00 |
| NLP scoring | – | 5–10 min | after GDELT |
| **Total** | **≈7k–15k requests + 1.4 GB** | **≈1.5–3 h wall-clock** (parallel per source; DOC API is the long pole) | |

**One-off backfills:**
- **DART:** 2015→ via `fnlttMultiAcnt` (≈28 calls per period × 44 periods ≈ 1.3k calls), then `fnlttSinglAcntAll` for the full universe over about 6 days (112k calls at 20k/day).
- **EDINET:** 10 years × 365 list calls plus about 8k docs/year.
- **KRX Open API:** ≈8k calls (1 day under the 10k cap).
- **BaoStock:** full history, ≈5.5k calls.
- **GDELT:** raw files per year, or BigQuery.
- **FRED ALFRED:** all vintages for ≈30 revised series.

### 9.3 Free keys to register

| Env var | Service | Signup | Priority |
|---|---|---|---|
| `FRED_APIKEY` | FRED/ALFRED | https://fred.stlouisfed.org/docs/api/api_key.html | **Must** (currently empty) |
| `EDINET_APIKEY` | EDINET API v2 | https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1 (SMS verification) | **Must** |
| `OPENDART_APIKEY` | OpenDART | https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do | **Must** |
| `KRX_OPENAPI_KEY` | KRX Open API (apply per service; approval ≈1 day) | https://openapi.krx.co.kr | **Must** for KR prices |
| `BOK_ECOS_APIKEY` | Bank of Korea ECOS | https://ecos.bok.or.kr/api/#/AuthKeyApply | Should |
| `BANXICO_TOKEN` | Banxico SIE | https://www.banxico.org.mx/SieAPIRest/service/v1/token | Should |
| `JQUANTS_APIKEY` | J-Quants (free tier is 12-week delayed; Light ¥1,650/mo for tanshin) | https://jpx-jquants.com/ | Optional / paid |
| `TUSHARE_TOKEN` | Tushare Pro (free = unadjusted daily only) | https://tushare.pro/register | Optional |
| `ESTAT_APPID` | Japan e-Stat (CPI, labour; alternative to IMF) | https://www.e-stat.go.jp/api/ | Optional |
| `KRX_ID` / `KRX_PW` | KRX Data Marketplace login (only if using pykrx) | https://data.krx.co.kr | Optional (avoid: pandas<3 pin) |

**No key needed:** BoJ API, ECB, Eurostat, BoE, ONS, OECD, IMF, World Bank, BIS, DBnomics, BCB, BCRA, BCRP, datos.gov.co, mindicador, GDELT, BaoStock, AKShare, CNINFO, SSE/SZSE, HKEX/HKEXnews, TWSE, NSE archives, ASX.

---

## 10. Cross-cutting pitfalls (Windows, encoding, geo)

1. **Encodings.**
   - EDINET code list is cp932 with a metadata first line, and EDINET CSVs are UTF-16LE TSV.
   - KIND list is EUC-KR HTML disguised as `.xls`; Naver XML is EUC-KR.
   - The SZSE xlsx is labelled GBK (pandas reads it fine).
   - Set `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`, because the Windows console is cp1252 and crashed on `～`.
2. **Identifiers.**
   - JP codes are 4-char alphanumeric (EDINET and TDnet use 5 digits with a trailing 0).
   - KR has `stock_code` (6) versus DART `corp_code` (8).
   - CN uses `sh.600519` (BaoStock), `sh600519`/`SH600519`/`600519` (AKShare) and `600519.SS` (Yahoo).
   - HK has `00700` versus Yahoo `0700.HK` versus HKEXnews `stockId` (7609) versus DI `sid` (6893).
   - Keep a crosswalk table keyed on ISIN where available (HKEX, NSE, KRX and SSE provide ISINs or English names).
3. **Point-in-time.**
   - Always store `knowledge_ts` from the source: EDINET `submitDateTime`, TDnet time, DART `rcept_dt` (+1 session), Eastmoney `NOTICE_DATE`, BaoStock `pubDate`, HKEX `DATE_TIME`, GDELT `DATE`.
   - For FRED use ALFRED `realtime_start`. For other macro, use the nightly snapshot date.
4. **Library pins.** pykrx requires `pandas<3`, so isolate it. AKShare and BaoStock run on pandas 3.0.6 and Python 3.13 (verified).
5. **Timeouts and robustness.** AKShare has no timeouts (one call hung more than 25 min). GDELT DOC latency is 10–20 s with frequent 429s. Wrap every adapter with timeouts, retry with jitter, circuit breakers and per-host rate limiters.
6. **TLS on Windows.** `swsresearch.com` fails certificate verification (missing intermediate). Use `truststore.inject_into_ssl()` (Windows cert store) rather than disabling verification.
7. **Geo.**
   - China NBS blocks EU IPs, and Eastmoney is intermittently hostile.
   - OECD blocks VPN traffic; JPX blocks bot user agents (send a browser UA).
   - Run with the residential IP and no VPN.
8. **Schema drift.**
   - Eurostat HICP moved to ECOICOP 2 and the ECB moved `ICP` to `HICP` (Feb-2026).
   - IMF IFS was retired.
   - FRED dropped ISM and LBMA gold and truncated ICE BofA to 3 years.
   - Add a weekly "series freshness" monitor that alerts when the last observation is older than about 2× the expected lag.
9. **BCB SGS:** daily series need ≤10-year windows, and series 432 has future-dated rows.
10. **Licensing summary.**
    - Government and central-bank data: open with attribution.
    - GDELT: open with citation.
    - Exchange websites (TDnet, HKEX, KRX, NSE, SSE/SZSE) and scraper libraries (AKShare: Eastmoney/Sina/THS; Yahoo; Google News): **personal research use only, no redistribution, low request rates**.
    - J-Quants and Tushare: personal-use licences.

---

### Appendix A: test scripts
Throwaway scripts and outputs are in `%TEMP%\claude\…\scratchpad\asia_research\`: `t_probe.py`, `t_misc.py`, `t_ak*.py`, `t_fred*.py`, `t_fx.py`, `t_gkg*.py`, `t_gd*.sh`, `t_nlp.py`. They are not part of the project.
