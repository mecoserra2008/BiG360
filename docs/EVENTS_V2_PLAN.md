# Events & News v2: labelled, compact, historical (resumable plan)

**Goal (from the user):**
- Far more GDELT event coverage, including history, stored as efficiently as possible.
- Each event is labelled with the **securities it affects, sector, and country (or "GLOBAL" when it has none)**. Everything not needed for those labels is dropped.
- Much more past news, compressed so it can still be shown and stays relevant, at minimal storage cost.
- A much better **Global Events** page.

**Resume protocol:**
- Tick `[x]` with a one-line result.
- State lives in `data/state/events_v2.json`.
- Backfills run detached, newest-first and resumable, with a disk budget, and they never keep raw GDELT zips.
- Contract changes are additive only: bump `api.yaml` and log in `docs/CONTRACT_CHANGES.md`.

## Storage design ("label, then forget")
1. **Stream-and-discard.** Each 15-minute GDELT file (Events 2.0, Mentions, GKG 2.1) is downloaded to memory and parsed column-pruned. Raw files are never written to disk.
2. **Labelling at ingest.** A row is kept only if it gets at least one label:
   - **securities / issuers:** GKG V2Organizations + page title → OrgLinker, with the existing context/blocklist rules, a link confidence, and only rows at ≥ 0.5 kept;
   - **sector:** from linked issuers, plus a GDELT-theme → sector map (e.g. ENV_OIL → Energy, ECON_BANKRUPTCY → affected sector, TAX_FNCACT…);
   - **country:** ISO2 from action geography, actors and V2Locations, with FIPS converted to ISO2; "GLOBAL" when none applies;
   - **market theme:** about 40 market-relevant themes (inflation, rates/central banks, sanctions, tariffs/trade, strikes/labour, M&A, bankruptcy, cyber, natural disaster, pandemic, conflict/military, elections, protest/unrest, energy supply, commodities, regulation/antitrust, product recall, …), stored as a **bitmask (int64)**.
3. **Compact columnar encoding:**
   - dictionary-encoded ints for labels, domains and CAMEO codes;
   - int8/int16 scaled tone and Goldstein;
   - uint16 mentions;
   - a URL hash instead of the URL; the cleaned title kept only on the story table;
   - Parquet ZSTD, sorted by (label, date) for maximum compression.
4. **Three tiers with retention:**
   - **T1, daily aggregates, kept forever:** country × theme × day, sector × theme × day, issuer × day (counts, tone, Goldstein, mentions, attention z). Tiny.
   - **T2, labelled event and article rows:** full detail for the last N days (default 180). Older rows are kept only if salient (top-k per label per day by mentions/sources, plus any row with |tone| or Goldstein extreme); the rest are folded into T1.
   - **T3, stories (news compression):** near-duplicate articles are clustered with SimHash/MinHash on the title within a time window. One row per story: canonical headline, first/last seen, n_articles, n_sources, top domains, mean tone/sentiment, labels, theme mask, and up to 3 representative URLs. Articles older than N days are dropped and only their story remains.
5. **Applied to all news** (Polygon, RSS, GDELT article titles) through the same story layer. FinBERT/XLM-R sentiment is computed once per story, not per article.

## Steps
- [x] E1 **Label taxonomy & maps:** done 2026-10-12 - `app/events_v2/{taxonomy,dicts,context}.py`; 41 themes (bitmask), GKG->theme (strong / broad x2-3 mention rule), theme->sector, CAMEO->theme, FIPS->ISO2, GLOBAL rule, alias table 23.7k aliases / 19.1k issuers (incl. SSE/SZSE English names, curated KR/CN/JP); contract `event_labels`.
  - market-theme taxonomy with a GKG theme → market theme map; theme → sector map; FIPS → ISO2; OrgLinker reuse plus extended aliases from issuers (incl. eu_broad and the new markets); a "GLOBAL" rule. Contract `event_labels` (dictionary tables).
- [x] E2 **Fast streaming parser:** done - `app/events_v2/parse.py` + `pipeline.Streamer`; in-memory, 9/27 GKG cols, prefilter, 16 processes; ~85-140 CPU-s per GDELT day (~6-9 s wall on 16 workers) but download-bound at the polite 4 req/s: ~2.5-3 min/day (target 1 day/min NOT met; it needs ~10 req/s).
  - column-pruned, multiprocess (≈ cores − 4), regex prefilter on V2Organizations/themes before full parse; throughput target ≥ 1 GDELT day per minute.
- [x] E3 **Compact tables + tiering:** done - 5 new contracts, day-file store (`store.py`), jobs events_agg (T1 z + compaction) and events_retention (salient-only after 180 d).
  - `gdelt_event_labels` (T2 events), `gdelt_article_labels` (T2 GKG articles), `event_daily_agg` (T1), `news_stories` (T3), `story_members` (T2, pruned by retention). A retention/compaction job runs nightly.
- [x] E4 **Story clustering for all news sources;** done - `stories.py` (MinHash-LSH Jaccard>=0.6, 72 h linking) + job story_cluster (`story_job.py`: Polygon/RSS/DOC merge + FinBERT/XLM-R per story).
  - sentiment per story; relevance per label.
- [x] E5 **Pilot backfill (last 90 days)** done 2026-10-13 03:29 (2026-07-14..10-11, 90/90 days, 0 missing files). Full T2 12.9 MB/day (stories 7.9, articles 2.4, members 1.9, events 0.63, T1 0.12); long-term after retention 2.15 MB/day (stories 1.6, events 0.29, articles 0.18, T1 0.10). Keep: events 21%, GKG articles 31.5%, 10.4% of kept articles issuer-linked; 1.42 articles/story. 23 days/h uncontended (download-bound, 4 req/s), 17 days/h overall incl. build_serving_db pauses. Spot check: security links 82% (49/60), country 84%, theme 68%, sector ~68%. Projection 2020->2026-07: ~3.7 TB streamed, ~4.3-6 days, ~6.5 GB long-term + ~2.3 GB rolling 180-d full T2.
  -, then measure bytes per day per tier, throughput, label precision on a 100-row spot check, and the projected cost of a full backfill (2015-02 → now for GDELT 2.0; optionally GDELT 1.0 daily events back to 1979 for country-level T1 aggregates only).
- [ ] E6 **Deep backfill (APPROVED by the user 2026-10-12):**
  - **Window:** 2020-01-01 up to the start of the pilot window. GDELT 2.0 Events, Mentions and GKG. No GDELT 1.0.
  - **Extended 2026-10-23 (Roadmap v3 N1):** the campaign `gdelt_e6` now targets 2015-02-19 (the start of GDELT 2.0); days already checkpointed are skipped, so it continues newest-first below 2020 without a restart (`contracts/coverage.yaml`).
  - **Batches:** each batch runs the full cycle of download → parse/label → write processed tables → verify (row counts, checksums, state checkpoint) → **delete that batch's raw files** before the next batch starts.
  - **Hard cap: never more than 20 GB of unprocessed GDELT on disk at once.** A raw staging directory is enforced by a byte-budget guard. If the next download would exceed 20 GB, wait or shrink the batch; never exceed it. Batch size defaults to about 7 GDELT days (~10 GB GKG + events), so one batch can be downloading while another is processing, still under 20 GB in total.
  - **Run mode:** newest-first, detached background process (survives Claude session limits), resumable from a checkpoint per batch, throttled and polite to data.gdeltproject.org.
  - **Progress:** logged to `data/logs/events_v2/backfill.log`, with batch status in `data/state/events_v2.json`.
  - **After each batch:** T1/T2/T3 tables updated, so data is usable as it lands. Compaction and retention rules apply to older batches.
- [x] E7 **API (events v2):** done - `app/api/routers/events_v2.py`, api.yaml 1.16.0 (8 paths, EventsV2* schemas), serving tables ev2_*; tests `backend/tests/events_v2/`.
  -
  - map (country intensity/tone with a theme filter and a time slider)
  - timeline (theme × time)
  - story feed (filters: country, sector, issuer/security, theme, tone, salience)
  - story detail
  - company / sector / country event panels
  - an "event shock" detector (attention z / tone break per label)
  - event-study hooks for Model Studio (event dates per label)
- [x] E8 **Global Events UI redesign:**
  - world map with a time slider and theme filter
  - "what's moving": top stories by salience
  - theme heatmap: country × theme
  - sector and company impact panels
  - story drill-down with sources
  - shock alerts, and a "send to Model Studio event study" button
  - plus Company 360 / country page event panels using stories
- [ ] E9 **Storage housekeeping (user decision needed):** raw ESEF/OAM annual-report packages (`data/raw/oslo_newsweb` = 8.9 GB of zips, likely the same for other OAM sources) are already parsed to compact xBRL-JSON. Proposed: keep the parsed JSON and delete or zstd-archive the original zips. Not to be done without approval.
