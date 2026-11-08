# CFTC positioning (COT) — plan (agent: cftc)

Source: CFTC Public Reporting Environment (Socrata, publicreporting.cftc.gov) — official only.
Datasets (verified live 2026-10-19): legacy_fut 6dca-aqww (1986+), legacy_comb jun7-fc8e, disagg_fut 72hh-3qpy (2006-06+),
disagg_comb kh3c-gbw2, tff_fut gpe5-46if (2006-06+), tff_comb yw9f-hn96, cit (Supplemental) 4zgm-a668 (2006+).
PIT: knowledge_ts = Socrata row `:created_at` (observed release, rows loaded after 2022-09-13 bulk load); otherwise
scheduled release = report Tuesday + 3 days 15:30 ET, moved to next business day after a Wed-Fri federal holiday;
shutdown windows 2013-10 / 2018-12..2019-01 conservative (knowledge_date_estimated=true).

Steps (state: data/state/cftc.json)
1. [x] contracts: cftc_markets, cftc_positions, cftc_signals (+ jobs, api.yaml, CONTRACT_CHANGES)
2. [x] app/ingestion/cftc.py (+ curated mapping app/ingestion/cftc_mapping.py) + tests/fixtures
3. [x] app/analytics/cftc_signals.py + tests
4. [x] serving tables + router /api/v1/positioning/* + contract tests
5. [x] frontend page /macro/positioning(+/:code), nav, instrument link, Studio source `cftc`
6. [x] run backfill + signals + build_serving_db; verify 8040/5181 headless; restart user API :8010

Status 2026-10-19: backfill done (5.19 M position rows, 999 markets, 9 min / 23 requests); signals 6.24 M rows (68 s);
Studio source `cftc` (field market:report_type:category:metric). Step 6 done: serving snapshot SV20261019T003714FA03 (cftc tables 8.3 s), headless check 0 console errors, :8010 restarted.
Resume: `cftc_cot` incremental (watermark - 28 d) -> `cftc_signals` -> build_serving_db.
