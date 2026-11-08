# Robots.txt compliance (COVERAGE_V3 A9, 2026-11-07)

Every external host the backend requests was enumerated and its `robots.txt` was evaluated for our User-Agent and the
path prefixes we request. Where a source disallows us, it was switched to an allowed source or disabled, as the user
decided ("switch each robots-disallowed source to an allowed one, or disable it", docs/COVERAGE_V3_PLAN.md A9).
Data ingested before a switch stays in the lake, labelled with its original source.

## How the audit works

| Step | What | Code |
|---|---|---|
| Enumerate | Every http(s) URL in a string literal or f-string of `backend/app` (docstrings and comments skipped); the curated RSS feeds (`config/news_feeds.yaml`, rejected feeds excluded); requests made inside libraries (yfinance, AKShare: `LIBRARY_REFS`); data-driven download URLs recorded in the adapters' state files (`data/state/*.json` `doc_url`, e.g. OAM packages). A path prefix is the literal path up to its first template field or query. XML namespaces, terms pages and UI metadata links are classified as non-requests or links. | `python -m app.ops.robots_compliance scan --out DIR` |
| Fetch | One `GET /robots.txt` per host, at most 1 request per second, no retries, with the app's User-Agent `BiG-Equity-360/1.0 (+local research; <contact>)`. 178 hosts, fetched 2026-11-07 01:12 UTC (re-tried 10:06 UTC for the 2 unreachable and the 2 data-driven hosts found later). | `... fetch --out DIR` |
| Evaluate | RFC 9309: the group of our product token `BiG-Equity-360` (case-insensitive), else `*`; the longest matching rule wins and `Allow` wins a tie; `*` and `$` wildcards; percent-decoding. HTTP 4xx for robots.txt means no restrictions; 5xx or a network error means unreachable, reported as `unknown`. Rules deeper than a requested prefix are flagged for review. | `... report --out DIR --doc docs/it/ROBOTS_COMPLIANCE.md` |
| Enforce | `app.core.robots_policy.BLOCKS` is a deny list (host + path prefix). `app.core.http` (sync, async and streaming downloads) checks it before any network I/O and again on every redirect hop (httpx request hook), and raises `SourceDisallowed` (an `HttpError`, status 451, reason `source_disallowed`). Code that builds its own httpx client (the CC-NEWS backfill, the RSS poller, the JPX / HKEX / CNINFO / NSM earnings-date clients) calls `robots_policy.guard()` or attaches `robots_policy.httpx_request_hook`. `/robots.txt` itself is always allowed. Entries with `enforce=False` are listed only, because another item or the owner decides. | `app/core/robots_policy.py`, `app/core/http.py` |

**Reason code `source_disallowed`** (sync code `no_source`). It appears in:
- `app.core.availability` and `contracts/field_availability.yaml` (`reason_codes`, rule `r_a9_jp_tdnet`);
- `contracts/coverage.yaml` `known_gaps` `seed:a9:*` (unfillable, so the gap filler never retries them);
- the startup sync, where a disabled job is reported as `no_source` with the note and is never planned;
- the jobs themselves, which return `skipped` (not retryable) with the note.

## Decisions

| Source (owner job / stream) | robots.txt rule | Action | Data kept |
|---|---|---|---|
| **TDnet** `www.release.tdnet.info` (`tdnet_disclosures`, realtime `tdnet`) | `User-agent: *` `Disallow: /` | **Disabled.** The job returns skipped, the stream is off, and the host is blocked. JP earnings dates come from the JPX schedule workbooks (`www.jpx.co.jp`, robots allows everything) plus Yahoo. JP statements and periodic-report events come from EDINET (`api.edinet-fsa.go.jp`: its robots.txt is an HTML page with no rules; `disclosure2dl` has no robots.txt). Both are already wired: `app.earnings.fetch.fetch_jpx` and `app.earnings.events.periodic_from_filings_index`. The other TDnet references in the code are labels or stored document links, not requests. | 10,752 `filings_index` and 553,903 `fundamentals_raw` rows (2026-09-10 .. 2026-11-06), `source_id tdnet` |
| **Bank of England IADB** `/boeapps/database/_iadb-FromShowColumns.asp`, `/boeapps/iadb` (macro_national `boe`, cb_balance_sheets `boe`) | `Disallow` on both paths (IIS serves paths case-insensitively, so the lower-case variant the old fetcher used is the same resource; the policy blocks all of `/boeapps/database/`) | **Switched**, keeping the IADB series ids. Gilt zero-coupon yields (IUDSNZC / IUDMNZC / IUDLNZC) come from the BoE yield-curve archive under `/-/media/` (the W3 source, `app.rates.sources`). Bank Rate (IUDBEDR) comes from the MPC voting record `mpcvoting.xlsx` (`/-/media/`) as a step series on XLON sessions, with BIS WS_CBPOL before the first recorded decision. SONIA (IUDSOIA) comes from FRED's republication, IUDSOIA. Series with no allowed source are no longer fetched. The par yields IUDSNPY / IUDMNPY / IUDLNPY are catalog `retired`: the zero-coupon series of the same tenor replace them over the full history. The Bankstats codes LPMVQJW / LPMBC57 / LPMVTVX / IUMBV34 have no replacement, so they are catalog `frozen`: they stay macro-panel candidates with their stored history and the normal staleness ordering, their title is labelled `[frozen since 2026-11-07: source_disallowed]`, and macro_audit / macro_freshness do not flag them. The weekly balance sheet `cb_balance_sheets boe` is frozen too. Its stored rows are snapshotted once to `data/raw/macro/cb/boe_iadb_frozen.parquet`, because the table is overwritten nightly. A read error never empties the BoE set: the job then upserts, so the stored rows stay. | all stored IADB rows (frozen series keep their history; 3,908 balance-sheet rows, backup in `scratchpad/cov3/A9/backup/`) |
| **SEC `/cgi-bin`** `www.sec.gov/cgi-bin/browse-edgar` (realtime `edgar`) | `Disallow: /cgi-bin` | **Switched** to the SEC XBRL RSS `/Archives/edgar/xbrlrss.all.xml` (allowed): the latest 200 XBRL filings, updated every 10 minutes, polled every 180 s in US hours and 600 s otherwise. 8-K item numbers come from each kept 8-K's `.hdr.sgml` header (`/Archives/edgar/data`, allowed), so the live Item 2.02 earnings signal keeps working. An 8-K is emitted only after its header is read. 8-Ks past the 40-headers-per-poll cap, or whose header request failed, wait and are retried on the next polls. After 1 h they are emitted without items, flagged `items_unresolved`. **Trade-offs:** Forms 3/4/5 and Schedule 13D/G carry no XBRL, so they are no longer live; they arrive with the nightly SEC jobs (daily index). The feed holds only the latest 200 XBRL filings, so a burst bigger than that between two polls (heavy 10-Q days) drops filings from the live path. They still arrive with the nightly jobs. Each such overflow is logged and counted in the stream detail (`feed overflows N`). All other SEC paths we use are allowed. | n/a |
| **BEA RSS** `apps.bea.gov/rss/` (realtime fallback list) | `User-agent: *` `Disallow: /` (only /api/, /iTable/ ... allowed) | **Disabled.** It was removed from the no-config fallback; `config/news_feeds.yaml` had already rejected it. BEA releases come through the macro release calendar (`www.bea.gov` schedule, allowed). | - |
| **Google News RSS** `news.google.com/rss/` (`news_ingest` `google_news` option, opt-in, off) | `Disallow: /` except home / topics pages | **Disabled.** The option is now a no-op and is reported as `skipped`. | - |
| **ISTAT calendar** (ICS on `calendar.google.com`; `istat_cal`) | `Allow: /$`, `Disallow: /` | **Disabled:** the fetcher raises `SkipSource`. istat.it publishes no other machine-readable calendar (checked: the press-calendar pages only embed the Google calendar). | stored calendar entries |
| **Jin10** `cdn.jin10.com` (AKShare `macro_china_shibor_all`) | `Disallow: /` | **Switched** to the SHIBOR administrator's own publication, CFETS / chinamoney `ShiborHis` (`www.chinamoney.com.cn`, no robots.txt). The other AKShare functions we call (Eastmoney, NBS, Sina, 10jqka, CNINFO, SSE / SZSE / BSE) are allowed. | all stored rows |
| **Common Crawl** `data.commoncrawl.org` (CC-NEWS backfill, campaign `ccnews_history`) | `User-Agent: *` `Disallow: /` | **Disabled.** No index or WARC is requested. The campaign probe reports done with reason `source_disallowed`, so the filler stops starting it. `--finalize-only`, `--relink` and `--local-dir` still work. Common Crawl documents this host as its download endpoint; re-enabling needs written confirmation from Common Crawl and a change to the policy. | merged ccnews story-layer rows |
| **BaFin directors' dealings** `portal.mvp.bafin.de` (`eu_pdmr_bafin`) | `User-agent: *` `Disallow: /` | **Disabled**. No other free machine-readable source for DE PDMR notices exists. | 292 `filings_index` notices (2025-10-14 .. 2026-11-06) and their insider rows |
| **DILA info-financière** `www.info-financiere.gouv.fr/api/` + package mirror `fr.ftp.opendatasoft.com` (`dila_fr_filings`, `eu_expansion` census) | `Disallow: /api/`; mirror `Disallow: /` | **Disabled.** French ESEF annual reports continue to come from filings.xbrl.org (`esef_xbrl_org`, no robots.txt). | 272 filings, 110,693 `fundamentals_raw` rows (2021-03 .. 2026-09) |
| **Taiwan MOPS** `mopsov.twse.com.tw`, `doc.twse.com.tw` (`mops_tw`) | `User-agent: *` `Disallow: /` | **Listed only.** Item B6 replaces MOPS with the TWSE / TPEx OpenAPI. | - |
| **Yahoo Finance API** `query1/query2.finance.yahoo.com` (yfinance: prices, fallback statements, earnings dates, quotes) | `User-agent: *` `Disallow: /` | **Listed only: OWNER DECISION.** Yahoo is a core source the user chose (Polygon + yfinance), and plan items B4–B8 build on it. Switching it off would remove most non-US prices. It is already labelled unofficial / personal use (docs/it/DATA_LICENSING.md §1, §2.1), and regulator sources take precedence. Not switched off by A9. | - |
| **IMF SDMX API** `api.imf.org` | robots.txt answers HTTP 502 (API gateway, twice) | **Kept.** This is the IMF's documented programmatic interface. Re-check robots.txt. | - |
| **IBGE** `servicodados.ibge.gov.br` (`ibge_cal`) | unreachable (connect timeout, twice) | **Kept.** The API times out from this network as well, so `ibge_cal` fails until it is reachable. Re-check robots.txt before relying on it. | - |

## Validation (small samples, scratch data dir, no lake writes)

- **BoE.** I ran `macro_national.fetch_boe` end to end on all 12 catalog series. It used a scratch `BIG360_DATA_DIR` holding a copy of the W3 archive and made 4 requests, all policy-checked: the latest-yield-curve zip, mpcvoting.xlsx, BIS D.GB and FRED. Compared with the stored IADB values:
  - **IUDBEDR:** 9,314 / 9,314 days identical. The day set is identical too, once the XLON calendar clamp was fixed (a start before 1990-01-02 used to drop to weekdays).
  - **IUDSOIA:** 7,541 / 7,541 identical.
  - **IUDSNZC:** 9,291 / 9,291 identical.
  - **IUDLNZC:** 9,146 / 9,146 identical.
  - **IUDMNZC:** 9,280 / 9,291 identical at 4 dp. The 11 others are 2019 days where the IADB repeated a stale 1.0364. The 22 stored-only days are October 2026: the archive copy predates the BoE's monthly update and the latest zip holds November only.
  - **Month-boundary gaps (fixed after review).** The same October hole reached the live lake. The 2026-11-07 02:11 UTC nightly first wrote IUDSNZC / IUDLNZC from the 2026-10-31 archive copy (ending in September) plus the November-only latest zip, so 22 sessions (2026-10-01 .. 10-30) are missing. The old rule (archive only when max(date) < first latest day − 4 days) would never have retried them. `_boe_yield_curve` now looks for missing XLON sessions in the 62 days before the current-month workbook, using the stored dates rather than the watermark. When the archive copy ends before the newest missing session, it is fetched again, at most once a day. A read-only dry run on the live lake finds exactly these 22 sessions for IUDSNZC and IUDLNZC and none for IUDMNZC. The BoE archive was updated on 2026-11-03 (one HEAD request, Last-Modified), so the first macro_national run after the restart fills the hole.
- **SHIBOR.** I ran `fetch_akshare_cn` on a one-year window: 2 CFETS requests, 252 / 252 days identical to the stored Jin10 rows, with no new-only and no stored-only days.
- **EDGAR.** The new parser read a live 200-item XBRL RSS sample (10-Q 73, 8-K 57, 20-F 3 kept). Times are converted from US/Eastern to UTC, and one 8-K header gave items 1.01 / 9.01.
- Tests: `tests/platform/test_a9_robots_compliance.py`, `tests/ingestion/test_a9_disabled_sources.py`, `tests/realtime/test_a9_edgar_xbrl.py`, `tests/ingestion/macro/test_a9_boe_shibor.py`, `tests/ingestion/macro/test_a9_boe_frozen_series.py`, `tests/macro_v3/test_a9_boe_frozen.py`, plus updates to `test_dila_fr.py` and `test_macro_parsers.py`.

## Open points

- **Restart needed.** The live supervisor children started on 2026-11-06 and still run the pre-A9 code. On 2026-11-07 the realtime `edgar` stream still polled `/cgi-bin` and `tdnet` still polled TDnet. The worker had already imported bafin_dd / dila_fr / calendar_sources. Restart the supervisor before the 02:00 UTC nightly DAG. Then check that `data/state/realtime_status.json` shows the edgar detail starting with `EDGAR XBRL RSS (A9: /cgi-bin disallowed)` and tdnet `disabled`. Also check that the next `eu_pdmr_bafin`, `dila_fr_filings` and `macro_calendar` (istat) runs are skipped with `source_disallowed` and no requests.
- **Yahoo** (owner decision, see above).
- **Crawl-delay** is a non-standard directive and is not enforced per host. Hosts that set it: www.destatis.de 30 s, www.fsma.be 30 s, dados.cvm.gov.br 10 s, www.eba.europa.eu 10 s, www.scmp.com 10 s, www.ecb.europa.eu 5 s, www.norges-bank.no 5 s, api.stlouisfed.org 2 s, www150.statcan.gc.ca 2 s, and 1 s for fred.stlouisfed.org, publicreporting.cftc.gov, www.datos.gov.co and www.ft.com. Our per-source limiters are about 1 request/s or slower, and those jobs make few requests, but an explicit per-host floor would be cleaner.
- **HTML robots.txt.** 16 hosts answer `/robots.txt` with an HTML page (evaluated as "no rules"). Some may be portals or bot pages rather than real answers.
- **Browser User-Agents.** Several adapters (afm, fca_nsm, hkex, jpx_list, dart_bulk, cnmv_es, emarket_it, twse_prices ...) send a browser User-Agent string instead of the app's. Robots evaluation used the `*` group, which applies to them either way, but identification is weaker.
- `contracts/macro_catalog.yaml` `sources.boe.url` still names the IADB endpoint. It is a source link shown in the UI and is never requested. Contract edits in this phase are additive, so the integrator should point it at the yield-curve page.
- `contracts/field_availability.yaml` is generated. Its rule `r19` (TDnet quarterly history) is replaced in code by `r_a9_jp_tdnet` and disappears at the next `python -m app.qa.completeness --write`.

## Re-run

```
python -m app.ops.robots_compliance scan   --out scratchpad/cov3/A9/scan
python -m app.ops.robots_compliance fetch  --out scratchpad/cov3/A9/scan     # cached hosts are skipped; --refetch for all
python -m app.ops.robots_compliance report --out scratchpad/cov3/A9/scan --doc docs/it/ROBOTS_COMPLIANCE.md
```

## Hosts, paths, verdicts

The Allowed? column gives the verdict for our User-Agent, and the Action column the decision taken. "Blocked at runtime" means the code still contains the URL, but `app.core.robots_policy` refuses the request before it leaves the process. Hosts we never request (namespaces, terms pages, UI links) are omitted.

<!-- ROBOTS-TABLE:START -->
<!-- generated by python -m app.ops.robots_compliance report (robots.txt fetched 2026-11-07..2026-11-07) -->
| Host | Paths (prefixes requested) | Allowed? | Action |
|---|---|---|---|
| apps.bea.gov | `/rss/` | **no** (Disallow: /) | blocked at runtime (app.core.robots_policy): BEA RSS removed from the realtime fallback list (config/news_feeds.yaml already rejected it); BEA releases via the macro release calendar (www.bea.gov schedule) |
| calendar.google.com | `/calendar/ical/t49o0tm9m8f5c72l2gamob8djs%40group.calendar.google.com/` | **no** (Disallow: /) | blocked at runtime (app.core.robots_policy): istat_cal (ISTAT ICS hosted on Google Calendar; ISTAT publishes no other machine-readable calendar) disabled; stored entries kept |
| cdn.jin10.com | `/data_center/reports/il_1.json` | **no** (Disallow: /) | switched, not requested any more: AKShare reads it inside the library (not through app.core.http), so macro_national never calls macro_china_shibor_all (AK_DISALLOWED); SHIBOR from CFETS / chinamoney ShiborHis (no robots.txt) |
| data.commoncrawl.org | `/` | **no** (Disallow: /) | blocked at runtime (app.core.robots_policy): CC-NEWS historical backfill (app.events_v2.ccnews, campaign ccnews_history) disabled; ccnews rows already merged into the story layer stay (source ccnews) |
| doc.twse.com.tw | `/server-java/t57sb01` | **no** (Disallow: /) | listed only (owner B6): MOPS document server (mops_tw); item B6 replaces MOPS with the TWSE / TPEx OpenAPI |
| fr.ftp.opendatasoft.com | `/datadila/INFOFI/` | **no** (Disallow: /) | blocked at runtime (app.core.robots_policy): as www.info-financiere.gouv.fr (the DILA package mirror) |
| mopsov.twse.com.tw | `/server-java` | **no** (Disallow: /) | listed only (owner B6): replaced by the TWSE / TPEx OpenAPI (item B6) |
| news.google.com | `/rss/search` | **no** (Disallow: /) | blocked at runtime (app.core.robots_policy): news_ingest google_news option (opt-in, off) switched off |
| portal.mvp.bafin.de | `/database/DealingsInfo/`, `/database/DealingsInfo/sucheForm.do` | **no** (Disallow: /) | blocked at runtime (app.core.robots_policy): eu_pdmr_bafin (BaFin directors' dealings) disabled; stored notices stay (source eu_pdmr_bafin); no other free machine-readable DE PDMR source |
| query1.finance.yahoo.com | `/`, `/v1/test/getcrumb`, `/v7/finance/quote` | **no** (Disallow: /) | listed only (owner user): OWNER DECISION: Yahoo (yfinance) is a core price / fallback source chosen by the user and used by plan items B4-B8; labelled unofficial / personal use (docs/it/DATA_LICENSING.md 2.1); not switched off here |
| query2.finance.yahoo.com | `/v1/test/getcrumb`, `/v10/finance/quoteSummary/`, `/v8/finance/chart/`, `/ws/fundamentals-timeseries/v1/` | **no** (Disallow: /) | listed only (owner user): as query1.finance.yahoo.com |
| www.release.tdnet.info | `/inbs/` | **no** (Disallow: /) | blocked at runtime (app.core.robots_policy): tdnet_disclosures job and the realtime tdnet stream disabled; JP earnings dates from the JPX schedule + Yahoo, JP statements from EDINET; TDnet rows ingested before 2026-11-07 stay (source_id tdnet) |
| www.info-financiere.gouv.fr | `/`, `/api/explore/v2.1/` | **partly** (/api/explore/v2.1/catalog/datasets/flux-amf-new-prod -> Disallow: /api/; /api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records -> Disallow: /api/) | blocked at runtime (app.core.robots_policy): dila_fr (French OAM) disabled; FR ESEF annual reports continue from filings.xbrl.org (esef_xbrl_org); stored dila_fr filings stay |
| www.sec.gov | `/`, `/Archives/edgar/daily-index/`, `/Archives/edgar/data/`, `/Archives/edgar/full-index/`, `/Archives/edgar/xbrlrss.all.xml`, `/cgi-bin/browse-edgar` (+4) | **partly** (/cgi-bin/browse-edgar -> Disallow: /cgi-bin) | /cgi-bin blocked at runtime (app.core.robots_policy): the realtime EDGAR stream switched to the XBRL RSS + .hdr.sgml headers (/Archives/edgar, allowed); the deeper /Archives/edgar/data rule is one .gif (not used) |
| api.imf.org | `/external/sdmx/2.1/` | unknown (robots.txt unreachable) | kept: robots.txt answers HTTP 502 (API gateway, checked 2026-11-07 01:12 and 10:06 UTC); the IMF SDMX API is the documented programmatic interface; re-check robots.txt |
| servicodados.ibge.gov.br | `/api/v3/calendario/` | unknown (robots.txt unreachable) | kept: robots.txt and the API both time out from this network (2026-11-07, twice); ibge_cal fails until reachable; re-check robots.txt before relying on it |
| api.bcb.gov.br | `/dados/serie/bcdata.sgs.` | yes (no robots.txt) | none |
| api.bcra.gob.ar | `/estadisticas/v4.0/monetarias/`, `/estadisticascambiarias/v1.0/Cotizaciones/` | yes | none |
| api.beta.ons.gov.uk | `/v1/search/releases` | yes (no robots.txt) | none |
| api.company-information.service.gov.uk | `/` | yes (no robots.txt) | none |
| api.data.fca.org.uk | `/fca_data_firds_files`, `/search` | yes (no robots.txt) | none |
| api.e-stat.go.jp | `/rest/3.0/app/` | yes (no robots.txt) | none |
| api.edinet-fsa.go.jp | `/api/v2`, `/api/v2/documents.json` | yes (robots.txt is an HTML page (no rules)) | none |
| api.finra.org | `/data/group/fixedIncomeMarket/` | yes (no robots.txt) | none |
| api.fiscaldata.treasury.gov | `/services/api/fiscal_service/` | yes (no robots.txt) | none |
| api.gdeltproject.org | `/api/v2/doc/` | yes (no robots.txt) | none |
| api.gleif.org | `/api/v1/fuzzycompletions`, `/api/v1/lei-records` | yes | none |
| api.hkma.gov.hk | `/public/` | yes (no robots.txt) | none |
| api.massive.com | `/` | yes (no robots.txt) | none |
| api.news.eu.nasdaq.com | `/`, `/news/query.action` | yes (no robots.txt) | none |
| api.openfigi.com | `/v3/mapping`, `/v3/search` | yes (no robots.txt) | none |
| api.polygon.io | `/`, `/v1/marketstatus/now`, `/v2/snapshot/locale/` | yes (no robots.txt) | none |
| api.riksbank.se | `/swea/v1/Observations/` | yes (no robots.txt) | none |
| api.statistiken.bundesbank.de | `/rest/data/`, `/rest/data/BBSIS/` | yes (no robots.txt) | none |
| api.stlouisfed.org | `/fred`, `/fred/`, `/fred/series/observations` | yes; Crawl-delay 2 | none |
| api.worldbank.org | `/v2/country/` | yes (no robots.txt) | none |
| api3.oslo.oslobors.no | `/v1/newsreader`, `/v1/newsreader/` | yes (no robots.txt) | none |
| apis.datos.gob.ar | `/series/api/series/` | yes | none |
| apisidra.ibge.gov.br | `/values` | yes (no robots.txt) | none |
| asia.nikkei.com | `/rss/feed/nar` | yes | none |
| attachment.news.eu.nasdaq.com | `/` | yes (no robots.txt) | none |
| basic.10jqka.com.cn | `/new/` | yes | none |
| bdif.amf-france.org | `/back/api/v1/` | yes (robots.txt is an HTML page (no rules)) | none |
| bdm.insee.fr | `/series/sdmx/data/` | yes (no robots.txt) | none |
| bpstat.bportugal.pt | `/data/v1/domains/` | yes (robots.txt is an HTML page (no rules)) | none |
| bvmf.bmfbovespa.com.br | `/InstDados/SerHist`, `/InstDados/SerHist/COTAHIST_D` | yes (no robots.txt) | none |
| cdn.finra.org | `/equity/otcmarket/biweekly/` | yes (no robots.txt) | none |
| comtradeapi.un.org | `/data/v1/get/`, `/files/v1/app/`, `/public/v1/preview/` | yes (no robots.txt) | none |
| cpx.cbc.gov.tw | `/API/DataAPI/Get` | yes (no robots.txt) | none |
| dados.cvm.gov.br | `/`, `/dados/CIA_ABERTA/`, `/dados/CIA_ABERTA/CAD/`, `/dados/CIA_ABERTA/DOC/` | yes; Crawl-delay 10 | none |
| dart.fss.or.kr | `/dsab007/detailSearch.ax` | yes | none |
| data-api.ecb.europa.eu | `/`, `/service/data/`, `/service/data/ILM/`, `/service/data/IRS/`, `/service/data/MPD/`, `/service/data/YC/` | yes | none |
| data-dbg.krx.co.kr | `/svc/apis/sto` | yes (no robots.txt) | none |
| data.api.abs.gov.au | `/rest/data/` | yes (no robots.txt) | none |
| data.fca.org.uk | `/artefacts/` | yes | none |
| data.gdeltproject.org | `/gdeltv2`, `/gdeltv2/lastupdate.txt` | yes (no robots.txt) | none |
| data.norges-bank.no | `/api/data/`, `/api/data/GOVT_GENERIC_RATES/` | yes | none |
| data.sec.gov | `/`, `/api/xbrl/frames/`, `/submissions/`, `/submissions/CIK`, `/submissions/CIK0000320193.json` | yes (no robots.txt) | none |
| data.snb.ch | `/api/cube/`, `/api/cube/rendeiduebd/` | yes | none |
| data.stats.gov.cn | `/dg/` | yes (no robots.txt) | none |
| datacatalogapi.worldbank.org | `/ddhxext/DatasetDownload`, `/ddhxext/ResourceView` | yes (no robots.txt) | none |
| datacenter-web.eastmoney.com | `/api/` | yes | none |
| datacenter.eastmoney.com | `/api/` | yes | none |
| di.hkex.com.hk | `/di/` | yes (robots.txt is an HTML page (no rules)) | none |
| disclosure2.edinet-fsa.go.jp | `/WZEK0040.aspx` | yes (robots.txt is an HTML page (no rules)) | none |
| disclosure2dl.edinet-fsa.go.jp | `/searchdocument/codelist/Edinetcode.zip` | yes (no robots.txt) | none |
| download.companieshouse.gov.uk | `/` | yes | none |
| e00-expansion.uecdn.es | `/rss/mercados.xml`, `/rss/portada.xml` | yes | none |
| ec.europa.eu | `/eurostat/api/dissemination/`, `/eurostat/news/release-calendar`, `/eurostat/o/calendars/` | yes | none |
| eco.sapo.pt | `/feed/` | yes | none |
| ecos.bok.or.kr | `/api/StatisticSearch/` | yes | none |
| estadisticas.bcrp.gob.pe | `/estadisticas/series/api/` | yes (no robots.txt) | none |
| ews.fip.finra.org | `/fip/rest/ews/` | yes (no robots.txt) | none |
| fc.yahoo.com | `/` | yes (no robots.txt) | none |
| feeds.bbci.co.uk | `/news/business/rss.xml` | yes | none |
| feeds.bloomberg.com | `/markets/news.rss` | yes (no robots.txt) | none |
| feeds.content.dowjones.io | `/public/rss/RSSMarketsMain`, `/public/rss/RSSWorldNews`, `/public/rss/mw_topstories` | yes (no robots.txt) | none |
| filings.xbrl.org | `/`, `/api/filings` | yes (no robots.txt) | none |
| finance.yahoo.com | `/calendar/earnings` | yes | none |
| fred.stlouisfed.org | `/releases/`, `/series/` | yes; Crawl-delay 1 | none |
| guce.yahoo.com | `/consent` | yes | none |
| home.treasury.gov | `/resource-center/data-chart-center/interest-rates/` | yes | none |
| huggingface.co | `/api/models/ProsusAI/` | yes | none |
| kind.krx.co.kr | `/corpgeneral/corpList.do` | yes (robots.txt is an HTML page (no rules)) | none |
| mapping.gleif.org | `/api/v2/isin-lei/` | yes | none |
| markets.newyorkfed.org | `/api/rates/`, `/api/rates/unsecured/` | yes (no robots.txt) | none |
| mindicador.cl | `/api/dolar/` | yes (no robots.txt) | none |
| naciscdn.org | `/naturalearth/10m/cultural/`, `/naturalearth/50m/cultural/` | yes (no robots.txt) | none |
| newsweb.oslobors.no | `/`, `/message/` | yes (robots.txt is an HTML page (no rules)) | none |
| openapi.twse.com.tw | `/v1/opendata/t187ap03_L` | yes (no robots.txt) | none |
| opendart.fss.or.kr | `/`, `/api`, `/api/company.json`, `/api/corpCode.xml`, `/api/list.json`, `/cmm/downloadFnlttZip.do` (+1) | yes (robots.txt is an HTML page (no rules)) | none |
| pox.globo.com | `/rss/valor/` | yes (no robots.txt) | none |
| publicreporting.cftc.gov | `/resource` | yes; Crawl-delay 1 | none |
| query.sse.com.cn | `/sseQuery/`, `/sseQuery/commonQuery.do` | yes (no robots.txt) | none |
| quotes.sina.cn | `/cn/` | yes (no robots.txt) | none |
| registers.esma.europa.eu | `/solr/esma_registers_firds_files/select`, `/solr/esma_registers_upreg/select` | yes (robots.txt is an HTML page (no rules)) | none |
| sdmx.ine.gob.cl | `/rest/data/` | yes (no robots.txt) | none |
| sdmx.oecd.org | `/public/rest/data/` | yes (no robots.txt) | none |
| servicios.ine.es | `/wstempus/js/EN/` | yes (no robots.txt) | none |
| si3.bcentral.cl | `/SieteRestWS/SieteRestWS.ashx` | yes (robots.txt is an HTML page (no rules)) | none |
| ssr.finanstilsynet.no | `/api/v2/instruments/` | yes | none |
| stats.bis.org | `/api/v2/data/` | yes (robots.txt is an HTML page (no rules)) | none |
| stats.oecd.org | `/wbos/fileview2.aspx` | yes (no robots.txt) | none |
| totoro.banrep.gov.co | `/nsi-jax-ws/rest/data/` | yes (robots.txt is an HTML page (no rules)) | none |
| vip.stock.finance.sina.com.cn | `/corp/` | yes (no robots.txt) | none |
| webapi.cninfo.com.cn | `/api/` | yes (robots.txt is an HTML page (no rules)) | none |
| webapi.fsma.be | `/`, `/api/v1/`, `/api/v1/en/` | yes (robots.txt is an HTML page (no rules)) | none |
| www.1info.it | `/`, `/PdfViewer/` | yes (no robots.txt) | none |
| www.abs.gov.au | `/release-calendar/future-releases`, `/release-calendar/future-releases/` | yes | none |
| www.afm.nl | `/`, `/export.aspx`, `/nl-nl/sector/`, `/nl-nl/sector/registers/` | yes | none |
| www.atlantafed.org | `/-/media/Project/` | yes | none |
| www.bankofcanada.ca | `/`, `/content_type/press-releases/feed/`, `/core-functions/monetary-policy/key-interest-rate/`, `/valet/observations/` | yes | none |
| www.bankofengland.co.uk | `/-/media/boe/`, `/monetary-policy-summary-and-minutes/`, `/monetary-policy/upcoming-mpc-dates`, `/rss/news` | yes | IADB (/boeapps/database/, /boeapps/iadb; case-insensitive) blocked at runtime: the boe fetchers use /-/media/ files, /rss/news and /monetary-policy pages (allowed) |
| www.banxico.org.mx | `/SieAPIRest/service/v1/` | yes | none |
| www.bcb.gov.br | `/api/feed/sitebcb/`, `/api/servico/sitebcb/`, `/controleinflacao/copom` | yes | none |
| www.bde.es | `/webbe/es/estadisticas/` | yes | none |
| www.bea.gov | `/news/schedule`, `/news/schedule/ics/` | yes | none |
| www.biva.mx | `/`, `/storage/data-maintenance/`, `/storage/data-rs-cnbv/`, `/storage/information-send/` | yes | none |
| www.bls.gov | `/feed/cpi.rss`, `/feed/eci.rss`, `/feed/empsit.rss`, `/feed/jolts.rss`, `/feed/ppi.rss`, `/schedule/news_release/` (+1) | yes | none |
| www.boj.or.jp | `/`, `/en/mopo/mpmdeci/`, `/en/mopo/mpmsche_minu/`, `/en/rss/whatsnew.xml` | yes (no robots.txt) | none |
| www.bse.cn | `/nqxxController/` | yes (no robots.txt) | none |
| www.censtatd.gov.hk | `/api/get.php` | yes | none |
| www.cftc.gov | `/RSS/RSSGP/rssgp.xml` | yes | none |
| www.chinamoney.com.cn | `/ags/ms/cm-u-bk-currency/`, `/ags/ms/cm-u-bk-shibor/` | yes (no robots.txt) | none |
| www.cmfchile.cl | `/`, `/institucional/mercados/` | yes (no robots.txt) | none |
| www.cninfo.com.cn | `/new/`, `/new/commonUrl`, `/new/information/getPrbookInfo` | yes (no robots.txt) | none |
| www.cnmv.es | `/`, `/DocPortal/Posiciones-Cortas/NetShortPositions.xls`, `/ipp/2008`, `/portal/aldia/`, `/webservices/verdocumento/` | yes | none |
| www.data.gouv.fr | `/api/1/datasets/` | yes | none |
| www.datos.gov.co | `/resource/32sa-8pi3.json` | yes; Crawl-delay 1 | none |
| www.destatis.de | `/`, `/SiteGlobals/Forms/Suche/` | yes; Crawl-delay 30 | none |
| www.dmo.gov.uk | `/data/XmlDataReport` | yes (no robots.txt) | none |
| www.eba.europa.eu | `/rss.xml` | yes; Crawl-delay 10 | none |
| www.ecb.europa.eu | `/`, `/press/calendars/mgcgc/`, `/press/calendars/statscal/`, `/press/govcdec/mopo/`, `/rss/press.html`, `/stats/eurofxref/eurofxref-hist-90d.xml` (+1) | yes; Crawl-delay 5 | none |
| www.emarketstorage.it | `/`, `/sites/default/` | yes | none |
| www.esma.europa.eu | `/rss.xml` | yes | none |
| www.fca.org.uk | `/news/rss.xml`, `/publication/data/short-positions-daily-update.xlsx`, `/publication/documents/aggregated-current-net-short-positions.csv`, `/publication/documents/aggregated-historic-net-short-positions.csv` | yes | none |
| www.federalreserve.gov | `/feeds/press_all.xml`, `/feeds/speeches.xml`, `/monetarypolicy/fomccalendars.htm`, `/newsevents/pressreleases/monetary` | yes (no robots.txt) | none |
| www.fi.se | `/BlankningsRegister/GetAktuellFile`, `/BlankningsRegister/GetHistFile` | yes (no robots.txt) | none |
| www.fsma.be | `/`, `/en/de-shortselling` | yes; Crawl-delay 30 | none |
| www.ft.com | `/markets`, `/rss/home` | yes; Crawl-delay 1 | none |
| www.handelsblatt.com | `/contentexport/feed/finanzen`, `/contentexport/feed/top-themen` | yes | none |
| www.hkex.com.hk | `/Services/RSS-Feeds/News-Releases`, `/eng/services/trading/` | yes | none |
| www.hkexnews.hk | `/reports/bmn/ebmn.htm` | yes (no robots.txt) | none |
| www.ibge.gov.br | `/estatisticas/todos-os-produtos-estatisticas.html` | yes | none |
| www.ine.es | `/dynt3/Calendario/en/`, `/en/daco/daco41/` | yes | none |
| www.inegi.org.mx | `/app/api/indicadores/` | yes | none |
| www.insee.fr | `/en/agenda-diffusion`, `/en/information/1405540` | yes | none |
| www.iso20022.org | `/sites/default/files/` | yes (no robots.txt) | none |
| www.istat.it | `/en/information-and-services-for-users/journalists/` | yes | none |
| www.jornaldenegocios.pt | `/rss` | yes | none |
| www.jpx.co.jp | `/`, `/english/rss/markets_news.xml`, `/listing/event-schedules/financial-announcement/`, `/markets/statistics-equities/misc/` | yes | none |
| www.matteoiacoviello.com | `/gpr_files/` | yes (no robots.txt) | none |
| www.mof.go.jp | `/english/policy/jgbs/` | yes (no robots.txt) | none |
| www.newyorkfed.org | `/medialibrary/Research/Interactives/`, `/medialibrary/media/research/` | yes | none |
| www.norges-bank.no | `/en/rss-feeds/Press-releases---Norges-Bank/` | yes; Crawl-delay 5 | none |
| www.ons.gov.uk | `/`, `/releasecalendar` | yes (no robots.txt) | none |
| www.rba.gov.au | `/`, `/media-releases/`, `/rss/rss-cb-media-releases.xml`, `/schedules-events/board-meeting-schedules.html`, `/statistics/tables/csv/` | yes | none |
| www.riksbank.se | `/en-gb/rss/press-releases/` | yes | none |
| www.scmp.com | `/rss/12/feed`, `/rss/92/feed` | yes; Crawl-delay 10 | none |
| www.smv.gob.pe | `/`, `/SIMV/` | yes (robots.txt is an HTML page (no rules)) | none |
| www.snb.ch | `/public/en/rss/` | yes | none |
| www.sse.com.cn | `/` | yes (no robots.txt) | none |
| www.stat-search.boj.or.jp | `/api/v1/getDataCode` | yes (no robots.txt) | none |
| www.szse.cn | `/api/`, `/api/report/ShowReport` | yes | none |
| www.theguardian.com | `/uk/business/rss` | yes | none |
| www.tpex.org.tw | `/openapi/v1/mopsfin_t187ap03_O`, `/www/zh-tw/afterTrading/`, `/www/zh-tw/bulletin/` | yes (robots.txt is an HTML page (no rules)) | none |
| www.twse.com.tw | `/rwd/zh/afterTrading/`, `/rwd/zh/exRight/` | yes | none |
| www1.hkexnews.hk | `/`, `/ncms/script/eds/`, `/search/titleSearchServlet.do` | yes (no robots.txt) | none |
| www150.statcan.gc.ca | `/n1`, `/n1/dai-quo/ssi/`, `/n1/rss/dai-quo/`, `/t1/wds/rest/` | yes; Crawl-delay 2 | none |
<!-- ROBOTS-TABLE:END -->
