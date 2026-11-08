# Model Studio & price coverage: plan (resumable)

**Goal.** Turn Model Lab (today it only monitors the nightly XGBoost and Transformer) into a **Model Studio**: a general econometrics and ML workbench over every dataset in the lake. It must also run on much wider price coverage.

**Resume protocol** (same as the EU plan). Tick `[x]` plus a one-line result per item. Long jobs run detached with cursors under `data/state/`. Each workstream keeps a machine-readable state file: `data/state/model_studio.json` and `data/state/price_coverage.json`.

## Architecture
- **Dataset builder** (`app/modeling/datasets.py`). Any lake table can serve as a time series (one entity) or as a panel (entity × date). Sources: returns, prices, factor scores, fundamentals, ratios, macro, sentiment, insider signals, technicals.
  - Joins are point-in-time (`knowledge_date`).
  - Operators: lag, lead, diff, log, pct_change, rolling mean/std/z, winsorize, cross-sectional rank/z, resample (D/W/M/Q), dummies, interactions.
  - Universe, sector and region filters and date windows.
  - Every dataset is cached by a hash of its spec plus the snapshot id, so results are reproducible.
- **Model registry** (`app/modeling/methods/*`). Each method declares its parameter schema, the data shape it expects, its outputs and its diagnostics. Runs are asynchronous on the worker (process pool). Results live in `app_state` (`studies`, `study_runs`), artifacts under `data/modeling/`.
- **API.** `/api/v1/studio/...`:
  - catalogs: datasets, variables, methods
  - preview a dataset
  - submit a run, poll it, fetch results
  - save, compare and export studies
  - export as a Python script that reproduces the model
- **UI.** The "Model Studio" page:
  - builder: dataset → variables (with lag spinners) → method → parameters
  - results tabs: summary, coefficients with CIs, diagnostics, forecast fan, clusters (PCA scatter, dendrogram, cluster profiles), event-study plot, IRFs
  - saved studies, compare, export
  - presets
  - the existing ML monitor stays as a tab

## Methods by phase
**Phase A (now):**
- **Regression:** OLS/WLS/GLS with HAC and clustered SEs; distributed-lag regression; GLM (Gaussian, Binomial/logit, Poisson, Gamma, NegBin); quantile regression.
- **Time series:** ARIMA, SARIMA, ARIMAX/SARIMAX with order search by AIC/BIC; stationarity tests (ADF, KPSS); ACF/PACF.
- **Clustering:** K-means (elbow, silhouette), hierarchical/agglomerative (Ward, average, complete; dendrogram), PCA.
- **Diagnostics:** residuals, QQ, Breusch-Pagan, White, Durbin-Watson, Ljung-Box, VIF; rolling-origin out-of-sample evaluation.

**Phase B:**
- **Panel:** fixed and random effects (linearmodels), Fama-MacBeth, IV/2SLS.
- **Causal:** difference-in-differences (2×2, TWFE, event study with leads and lags, staggered adoption à la Callaway-Sant'Anna), synthetic control, event studies with CAR.
- **Multivariate time series:** VAR/VECM with IRFs and FEVD, cointegration (Engle-Granger, Johansen), Granger causality.
- **Volatility and regimes:** GARCH, EGARCH, GJR (arch); Markov-switching regression; state-space and Kalman filter (UnobservedComponents, dynamic betas).

**Phase C:**
- **Machine learning:** Lasso, Ridge and ElasticNet with CV; random forest and gradient boosting with SHAP.
- **More clustering and factors:** GMM, DBSCAN/HDBSCAN; a factor-model builder (time-series and cross-sectional).
- **Optional heavier models:** Bayesian (PyMC); deep sequence models (LSTM/TFT).
- **Workflow:** model comparison and leaderboard; scheduled re-runs with alerts.
- **Fixes:** the FT-Transformer architecture (gate or concatenate the lag path), then re-run the permutation test.
- **Integrations:** clustering results feed back into peer groups as an option.

**Guardrails throughout:** point-in-time datasets only; minimum sample-size checks; multiple-testing and overfitting warnings; reproducibility (spec hash, seed, snapshot id).

## Price coverage
Prefer exchange or official **bulk** sources over yfinance, which is currently throttling us (HTTP 429).

| Market | Source | Notes |
|---|---|---|
| China A-shares | BaoStock | full market, about 5,000 names, keyless |
| Taiwan | TWSE/TPEx OpenAPI daily all-stock files | |
| Brazil | B3 COTAHIST | full market, adapter already exists |
| Korea | KRX Open API | key pending; until then, the full KIND list via yfinance at a polite pace |
| Japan | full TSE list (JPX) via yfinance | polite pace |
| Hong Kong | full HKEX list via yfinance | |
| India | NSE bhavcopy | ToS decision pending with the user; not built until approved |
| All markets | ETFs, global indices, sector/factor ETFs, commodity futures, rates futures proxies via yfinance | |
| US delisted | Polygon reference tickers (active=false) plus grouped-daily history (2 years on the free tier) | partial survivorship-bias fix; full history needs CRSP or a paid source |

Every new listing gets identity, a taxonomy entry and price-quality flags. Universes: `full` grows by market, while `mvp_*` stays unchanged.

## Progress
- [x] A1 Dataset builder + method registry + run engine (backend) — app/modeling/{_core,datasets,registry,results,diagnostics,engine,store}.py: PIT (as-of on knowledge_date) dataset specs over 10 lake sources, D/W/M/Q, 26 transforms, cache per spec+snapshot; sync (<2 s est.) or async `studio_run` job (process executor, drains queue, cancel flag, timeout); app_state studies/study_runs.
- [x] A2 Phase A methods + diagnostics + tests — ols/wls/gls/distributed_lag/quantile/glm(5 families)/arima(SARIMAX + order search)/stationarity/kmeans/hierarchical/pca with BP/White/DW/LB/JB/VIF/cond, ADF/KPSS, rolling-origin OOS; guardrails (look-ahead, obs/param, singular, I(1), multiple testing); 8 presets; tests/modeling (35) all green.
- [x] A3 Studio API (contract additions in api.yaml) + script export — routers/studio.py, api.yaml 1.5.0 (16 ops, Studio* schemas), export.py embeds the dataset engine verbatim (scripts reproduce the 8 presets exactly).
- [x] A4 Model Studio UI v1 (frontend): /studio route (builder, results tabs, studies, compare, presets, export); all 8 presets + 3 hand-built models e2e in both themes, 0 console errors; 52 frontend tests. Mismatches → B0.
- [ ] P1 Price coverage: CN (BaoStock full), TW (OpenAPI), BR (COTAHIST full)
  - 2026-10-11 in progress: code `app.ingestion.price_coverage{,_ext,_lists,_delisted}` + `twse_prices`; detached tasks
    (launcher `scripts/price_coverage_launch.ps1 -Name <n> -TaskArgs "<task args>"`, logs/PIDs `data/logs/price_coverage/`,
    done-files `data/state/price_coverage/`, summary `data/state/price_coverage.json`): `cn --worker k --workers 3`
    (k=0..2, 5,224 A-shares from 2000, ~12 h), `tw --market twse|tpex` (archives 2004-/2007-, ~5 h), `br` (COTAHIST
    2000-2026, done). Seeds `seeds/price_coverage.csv` written (16.4k rows, all 6 markets). Next: ref_universe
    mode=rebalance keep_universes=mvp_us,mvp_global -> prices_finalize -> identity_resolve -> build_serving_db
    (not while the EU chain is in step 6 analytics).
  - 2026-10-12 status: BR done (COTAHIST 2000-2026, 381 listed tickers priced, median history from 2007); TW done
    (TWSE 2004-02-11.., TPEx 2007-01-02.., 4.72M + 3.27M bars, 28k ex-rights actions, 0 errors; 1,983 securities);
    CN 4,800 / 5,224 A-shares loaded (from 2000, 55k preclose-derived actions) when BaoStock BLACKLISTED the IP
    (login 10001011 "黑名单用户") after ~4 h with 4-6 concurrent sessions -> all CN workers stopped. Remaining 424
    (.SS tail): after the ban lifts run ONE worker: `scripts/price_coverage_launch.ps1 -Name cn_tail -TaskArgs
    "cn --worker 0 --workers 1"` (honours every p1_cn_w*.done), then prices_finalize regions=["CN"],
    identity_resolve, build_serving_db. Finalized CN/APAC/LATAM; identity_resolve + build_serving_db re-run.
- [x] P2 Price coverage: JP / KR / HK full lists (paced yfinance), ETFs, indices, futures
  - 2026-10-11 in progress: `scripts/price_coverage_p2_chain.ps1` (groups bench -> jp -> kr -> hk, one Yahoo pace,
    waits while the EU chain is in universe/prices, 429 backoff 10/20/40/60 min; re-run to resume).
  - 2026-10-11 downloads done, 0 throttle events: bench 223 (4 no data), JP 3,702 (17.4M rows), KR 2,651 (7 no data),
    HK 2,461 (12 no data); ref_universe rebalance (keep mvp_*) -> full 4,491 -> 20,682, all old issuer ids kept;
    prices_finalize JP/KR/HK/CA/EU done (JP 3,703 / KR 2,647 / HK 2,447 securities); US done 2026-10-11 22:40.
  - Benchmarks registered in identity (build_reference.benchmark_reference_rows, rebuilt each run): 247 instruments
    (48 index / 168 etf / 31 future; securities_master v3 security_type += index, future; synthetic issuer per
    instrument, ids IDX:^GSPC / ETF:SPY / IDX:CL=F), full Yahoo history (^GSPC from 1927, SPY 1993), excluded from all
    universes. Studio preview of IDX:^GSPC verified on a test API (port 8050).
- [x] P3 US delisted (Polygon), quality flags, identity + taxonomy for all new listings
  - 2026-10-11 polygon grouped-daily: 494 sessions (2024-10-11..2026-10-09; free-tier edge hit at 2024-10-10, 403),
    5.66M rows, 0 x 429. delisted: 768 inactive US tickers selected (>= 20 bars before delisting), 743 issuers / 753
    securities+listings registered inactive (valid_to = delisting date); all 753 priced in prices_daily (median 223 bars).
    DQ hook `price_coverage` (coverage:<MIC>, freshness:<region>, delisted_window) added.
  - Taxonomy: CN csrc 5,202, TW twse 1,976 (new TWSE/TPEx code map), JP tse33 3,662, KR ksic 2,184 (452 unknown KSIC
    names), BR cvm 204 (73 unknown), HK 2,433 unknown (no native code; needs yfinance .info later).
  - (earlier note) `price_coverage polygon` (2 y grouped-daily, shared 4/min bucket, newest first) running;
    then `price_coverage delisted` (inactive tickers -> seeds/us_delisted.csv -> additive inactive issuers/listings).
    TWSE taxonomy section added; finalize prices delisted listings inside their window.
- [x] B0 Contract fixes from the frontend — StudioTest.passed, ISO dates, run title/n_obs in lists, estimated_seconds/sync_budget_s (api.yaml 1.6.0, study_runs v2).
- [x] B  Phase B methods — panel (FE/RE/pooled/between/FD + Hausman, Driscoll-Kraay), fama_macbeth, iv2sls, did (2x2/TWFE/event study/Callaway-Sant'Anna), synthetic_control, event_car, var, vecm, cointegration, garch, markov_switching, state_space, tv_beta; core in app/modeling/_algos.py (embedded in exports); 7 new presets (15 total); plot kinds ts_band/event_path/irf/fevd/regime_prob.
- [x] B-UI Phase B rendering (frontend, api.yaml 1.6.0) — all 24 methods render (IRF grid + FEVD, event paths with pre-trend badge, group-time ATT heatmap, synthetic control + placebo spaghetti + donor weights, CAR/AAR, GARCH vol/VaR, regime probabilities, UCM components, Kalman beta bands, spread z-bands, Johansen/first-stage/Hausman tables, FM lambdas); builder treatment/events/IV-role/VAR inputs; 7 new presets + hand DiD + hand VAR e2e in both themes at 1440/1024, 0 console errors; 55 tests, build/lint clean.
- [x] B1 fixes — cointegration/info tests passed=null + conclusion; GARCH simulated sigma band; synthetic control on sector/region/country labels; event_car ticker + name; DiD event-study ATT delta-method SE; Markov header (api.yaml 1.7.0); GARCH / Kalman / Markov presets repointed to IDX:^GSPC.
- [x] C Phase C methods — regularized (Lasso/Ridge/ElasticNet, purged / walk-forward CV, coefficient paths), tree_ensemble (RF/HistGBM/XGBoost, SHAP, PDP, permutation importance), gmm, density_clustering (HDBSCAN/DBSCAN), factor_model (long-short quantile factors, GRS, second pass), leaderboard (identical folds, Diebold-Mariano), factor_ls dataset source, scheduled studies + drift alerts (job studio_scheduled), peer sets -> company peers; 6 new presets (21); core app/modeling/_ml.py embedded in exports. PyMC / deep sequence models: not built (see feasibility note in data/state/model_studio.json).
- [x] C1 fixes — catalog cache keyed by snapshot + catalog fingerprint (factor_ls listed); peer-set list returns members; HistGBM importance = permutation only (impurity column/series omitted, info note); StudioEvaluation.metrics widened; schedule none deletes the studio_drift alert; alert params carry schedule + drift_threshold (api.yaml 1.7.1).
- [x] C-UI Phase C rendering (frontend, api.yaml 1.7.0) — coef paths + CV curve + selected features, SHAP beeswarm / importance / PDP, GMM soft membership (opacity + probabilities), HDBSCAN noise, factor model (cum. factors, beta heatmap, alphas, GRS card, second-pass premia), leaderboard (ranked table, DM, cum. loss) + builder leaderboard mode via POST /studio/leaderboard; nested cv form, candidate checkboxes, factor_ls quantiles; study schedule/drift in Studies, studio_drift rows in Watchlist; peer sets (save from clustering, list/delete) + Company 360 Peers selector; B1 fixes shown; 6 new presets + hand leaderboard / factor model / GMM e2e in both themes at 1440/1024, 0 console errors; 57 tests, build/lint clean.
