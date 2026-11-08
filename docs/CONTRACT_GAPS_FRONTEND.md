# Frontend → API contract gaps (reported 2026-10-10)

The frontend found these fields missing from `contracts/api.yaml`. Rule for every fix: add **optional** fields only, never rename anything, bump the version and log the change in `CONTRACT_CHANGES.md`. Once a field is added, the frontend adapters in `frontend/src/api/adapters/` pick it up.

| # | Area | What the UI needs | Proposed fix |
|---|---|---|---|
| 1 | SecurityRef | ISIN, exchange name | add `isin`, `exchange_name` |
| 2 | Market overview | sovereign yield curves per region; unit scale; dated sparklines; market cap per sector (for the treemap); several regions per request | add `yield_curves[]` (tenor, yield, as_of + 1M/1Y-ago); a `scale` enum on macro (`percent`\|`fraction`\|`level`); `spark` as `[date, value]` pairs; `market_cap_usd` on SectorPerf; allow `region` as a list |
| 3 | Sectors | per-company KPI values for the distribution chart; composite median | add `values[] {security_id, ticker, value}` (cap ~500); `median_composite` on SectorSummary |
| 4 | Screener | a pinned list of column ids; ML expected excess return | add a `ScreenerColumnId` enum to the contract; add `ml_expected_excess_3m` |
| 5 | Company overview | peer median per factor; beta; shares outstanding; last filing date; FX rate to USD | add `peer_median` on FactorScore; `beta_1y`, `shares_outstanding`, `last_filing_date`, `fx_to_usd` |
| 6 | Forecast | WACC × terminal-growth sensitivity grid; consensus growth; several targets per trade plan | add `sensitivity_grid`, `consensus_growth` (nullable), `targets[]` |
| 7 | Moat | thresholds, pass/fail and history per component; market-share series; typed moat sources with strength; a UI state for insufficient data | add `threshold`, `passed`, `history[]` per component; `market_share_series`; `sources_of_moat[] {type, strength}`; `insufficient_data` shown as its own UI state |
| 8 | Insiders | anomaly score per transaction; cluster id per transaction | add `anomaly_score`, `cluster_id` on each transaction row |
| 9 | Sentiment | relevance score per news item | add `relevance` |
| 10 | GDELT events | headline; country-code standard; company links; 7-day change | add `headline` (source URL title); declare `action_country` as ISO2 (convert from FIPS); add `securities[] {security_id, ticker}`; add `change_7d` per country |
| 11 | Model | both models side by side; expected return with interval; confidence; train date; feature-set version; SHAP unit | return `models[]` per horizon (xgb, transformer, ensemble); add `expected_excess_return`, `interval`, `confidence`, `trained_at`, `feature_set_version`; document `shap_unit` |
| 12 | Model Lab | benchmark series; train/test window and Sharpe per fold; feature count | add `benchmark_series`; `train_start/end`, `test_start/end`, `sharpe` per fold; `n_features` |
| 13 | Regime/alerts | stagflation state; level threshold for score change; 1-day price-change rule | stagflation becomes its own UI state (frontend); score_change gets `mode: level\|delta`; new rule type `price_change_pct_1d` |
| 14 | System | expected cadence and region per freshness item; `partial` job status; freshness data actually filled | add `expected_cadence`, `region`; frontend maps `partial` to a warning state; the API fills `data_freshness` from job_runs/dq |
