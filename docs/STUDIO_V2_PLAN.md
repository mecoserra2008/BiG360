# Model Studio v2 (plan, approved 2026-11-05)

## Diagnosis (measured on 182 Studio runs and 10 backtest runs)
- **The wait in line counts against the estimate.**
  - Studio's median wait is about 5 s, about the same as its median run time, so a run takes about 2× its estimate end to end.
  - Backtests waited a median of 46 s, and one waited 66 minutes behind the weekly refresh. 6 of the 10 backtest runs were cancelled.
- **Estimator errors:**
  - `UNIVERSE_SIZE_GUESS["full"]=3000`, but the full universe is now ~26k securities.
  - Process spawn and import time (4–5 s) is not counted.
  - SHAP, bootstrap (B=199), placebo and sweeps are not counted.
- **CPU contention:** trees use `n_jobs=-1` / `8` while worker jobs are also running.

## S1. Speed (agent A)
1. **Interactive lane.** Add a `studio` supervisor child: a warm process with libraries pre-imported and a small pool, and its own Job Object with a RAM cap.
   - It serves Studio and backtest runs outside the worker's file queue and the heavy slot.
   - It is limited to cores−2, and it lowers its own priority while a heavy job runs.
2. **Learned estimates.**
   - Per method: regress log(run_s) on log(rows), n_x, params and the extras, fitted from run history. The static formula stays as the prior.
   - Use the real universe sizes from the snapshot.
   - The total adds queue and overhead.
   - UI: queue position, the job a run is blocked by, a live ETA, and the observed/estimated ratio tracked in diagnostics.
3. **Preview then full.** A fast run on a 10–20% sample (entities or dates) returns within seconds. The full run then refines it, with progress sent over SSE or polling.
4. **Heavy extras opt-in.** SHAP, bootstrap, placebo and sweeps are opt-in, with their cost shown. Presets keep their current outputs.
5. **Data efficiency.**
   - One dataset cache shared by Studio and backtests.
   - float32 for features.
   - `n_jobs` capped to the lane's cores.
   - Dataset builds use projection and predicate pushdown.

## S2. New models (agent B)
6. **Controls and causal methods:**
   - DML (partially linear model, interactive regression model; cross-fitted with sklearn learners)
   - propensity-score matching and IPW/AIPW with balance tables
   - regression discontinuity (sharp and fuzzy, with bandwidth selection)
   - a coefficient-stability table that adds controls one at a time (Oster δ)
7. **Advanced ML:**
   - LightGBM (new dependency)
   - Optuna tuning on purged/embargoed walk-forward CV (new dependency)
   - stacking with a DM test against the best base model
   - learning-to-rank (LambdaRank)
9. **Simple neural networks (torch, already installed):**
   - MLP
   - small LSTM/GRU for time series
   - autoencoder factors
   - training curves, early stopping and seed averaging
   - a mandatory linear baseline comparison

## S3. Probabilistic + UX (agent C, after A/B)
8. **Probabilistic models:**
   - BayesianRidge / ARD
   - Gaussian-process regression for small n
   - quantile GBM / LightGBM, and NGBoost (new dependency)
   - a hierarchical model with partial pooling by sector (empirical Bayes or Gibbs in numpy; no PyMC)
   - calibration plots and PIT histograms
   - conformal intervals for every regressor
10. **Guided builder:**
    - question → method + controls suggestion
    - look-ahead and leakage linter
11. **Compare runs + Model Registry** (roadmap v3 item S):
    - side-by-side runs with DM / Clark-West tests
    - promote to the registry
    - drift monitoring
12. **~40 recipes**, e.g. value factor, earnings drift (PEAD), rate shocks → banks, credit-spread regimes, momentum crash, quality-minus-junk.

## Rules
- api.yaml / jobs.yaml are additive, with version bump + CONTRACT_CHANGES + version assertion.
- Each new method gets a fixture test, a cost() function and a guide/glossary entry.
- New dependencies are pinned and added to the SBOM and DATA_LICENSING (software section).
- Token-lean agents: at most 2 in parallel.

## Progress
- [x] S1 Speed (1–5) — 2026-11-06: warm `studio` lane (app.modeling.lane), learned estimator (app.modeling.estimator), preview-then-full, extras opt-in, shared dataset cache + pushdown; api 1.48.0, jobs 46
- [x] S2 New models (6, 7, 9): dml, propensity, rdd, coef_stability, lightgbm, optuna_tune, stacking, ltr, mlp, rnn, autoencoder (2026-11-06; api 1.49.0; lightgbm 4.7.0 + optuna 5.0.0)
- [x] S3 Probabilistic + UX (8, 10–12): bayes_ridge (+ARD), gp_regression (n cap 3000), quantile_gbm, ngboost, hier_pool (EB / blocked Gibbs, R-hat / ESS); PIT / calibration / CRPS / pinball; cv.conformal_alpha on every CV regressor; guided builder + lint; compare with DM / Clark-West / equity curve; Model Registry + PSI / expanding-IC monitoring (studio_drift alerts); 42 recipes run on real data (median 5 s, max 187 s) (2026-11-06; api 1.50.0, jobs 47; ngboost 0.5.11)
