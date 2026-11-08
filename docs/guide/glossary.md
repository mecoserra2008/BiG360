# Glossary

Terms and acronyms used across BiG Equity 360, in alphabetical order. Each entry links to the guide section that uses or explains it. Terms in *italics* inside a definition have their own entry.

---

## A

- **ACI (adaptive conformal inference)** — An online correction of a *conformal* set: after each miss the working level is adjusted (`α_{t+1} = α_t + γ(α − err_t)`, γ = 0.005), so long-run coverage converges to target even when conditions change. Used for the next-bar and peer-group sets. See [Conformal prediction](16-methodology.md#adaptive-conformal-inference-aci).
- **ACM term premium** — The NY Fed's Adrian–Crump–Moench estimate of the extra yield investors demand for holding long Treasuries; used to adjust the US implied policy path. See [Model-based versus official data](16-methodology.md#model-based-versus-official-data).
- **Activism** — New Schedule 13D filings, 13D amendments and 13G → 13D switches (a passive holder becoming active). See [Smart Money](07-smart-money-insiders.md#activism-13d).
- **ADR (American depositary receipt)** — A US-listed certificate representing shares of a foreign company. Scores, technicals and fair value are computed on the home listing. See [ADRs versus home listings](17-good-practice.md#adrs-versus-home-listings).
- **ADV (average daily volume)** — Average traded value per day, usually over 20 sessions in USD; used for liquidity filters and the cost model. See [Backtest Lab](11-backtest-lab.md).
- **AIPW (augmented inverse-probability weighting)** — A doubly robust treatment-effect estimator: it combines a propensity model and outcome regressions and stays consistent if either is right. See [Methods catalogue](09-model-studio.md#studio-v2-controls-causal-advanced-ml-and-neural-networks).
- **Approximation** — A label for a simplified method from official inputs, such as the central-bank implied path. See [Model-based, experimental and approximation labels](17-good-practice.md#model-based-experimental-and-approximation-labels).
- **AR (abnormal return)** — A stock's return minus the return the *market model* predicts from the benchmark that day. See [Event studies](16-methodology.md#event-studies-and-abnormal-returns).
- **ARD (automatic relevance determination)** — Bayesian linear regression with one prior precision per feature, estimated from the data; irrelevant features are shrunk to zero. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **As of** — The date a value refers to (data as of) or the knowledge cut-off of a query (`as_of=` in BQL). See [Point-in-time data](16-methodology.md#point-in-time-data).
- **ATR (average true range)** — The average daily high-low range including gaps, over 14 sessions (ATR14); a volatility unit for levels, zones and the baseline "2×ATR" band. See [Technicals](05b-company-technicals.md#technicals).
- **Autoencoder** — A neural network that compresses inputs into a few latent factors and reconstructs them. In the Studio it is always compared with PCA using the same number of factors. See [Methods catalogue](09-model-studio.md#studio-v2-controls-causal-advanced-ml-and-neural-networks).

## B

- **Backfill campaign** — A long, resumable history download run by the *gap filler* (GDELT history, 13F history, ...), with progress on Data Health. See [Data Health](15-system.md#data-health).
- **Benjamini–Hochberg (BH)** — A multiple-testing correction that controls the expected share of false discoveries. See [Multiple testing](16-methodology.md#multiple-testing).
- **Beneish M-score** — An accounting ratio model that screens for possible earnings manipulation; in the app a recalibrated, model-based screen, not an accounting finding. See [Gate status](16-methodology.md#gate-status-of-every-model).
- **BHAR (buy-and-hold abnormal return)** — The compounded return of the stock minus the compounded return the *market model* predicts over a window. See [Event studies](16-methodology.md#event-studies-and-abnormal-returns).
- **BOCPD (Bayesian online change-point detection)** — A method that flags when the level of a stream (attention, tone) has shifted. See [Global Events](08-global-events-news.md#overview).
- **BQL** — The app's Bloomberg-style query language (`get / for / with / where`), compiled to point-in-time SQL. See [BQL and mnemonics](14-bql-mnemonics.md#bql-console).
- **Breakout (20 / 55)** — A close above the highest high (or below the lowest low) of the previous 20 or 55 sessions. See [Technicals](05b-company-technicals.md#technicals).
- **Brier score** — Mean squared error of probability forecasts; lower is better. See [Calibration metrics](16-methodology.md#calibration-metrics).
- **Brownian baseline** — The textbook probability that a driftless random walk hits one barrier before another; a baseline the first-passage model must beat. See [First-passage probabilities](16-methodology.md#first-passage-probabilities).

## C

- **Calibrated band / calibrated range** — The conformal band around the price on the Technicals chart for a given horizon and level (80 / 90 / 95%), validated per horizon. See [Conformal prediction](16-methodology.md#conformal-prediction).
- **Calibration** — Agreement between predicted probabilities and realised frequencies. "Calibrated over history" means on average over many past cases, not for this case today. See [How to read probabilities](17-good-practice.md#how-to-read-probabilities-and-bands).
- **CAR (cumulative abnormal return)** — The sum of daily *abnormal returns* over an event window such as [−1, +1]. See [Event studies](16-methodology.md#event-studies-and-abnormal-returns).
- **Catch-up** — A scheduled refresh that runs late because the PC was asleep or off at its scheduled time. See [System overview](15-system.md#system-overview).
- **CFTC COT (Commitments of Traders)** — The weekly CFTC report of futures positions by trader category, as of Tuesday and released on Friday. See [Positioning](02b-markets-cross-asset.md#positioning-cftc).
- **Clark–West test** — A test of equal forecast accuracy for nested models: it corrects the bigger model's MSPE for the noise of estimating extra parameters (one-sided). See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **Composite score** — The 0–100 score with a grade A–E that combines the 10 factor scores (Quality, Profitability, Growth, Value, Balance sheet, Momentum, Insider, Sentiment, Macro sensitivity, Moat) under a weight profile. **Descriptive**: a peer-relative ranking, not a forecast or a buy / sell signal. Out-of-sample tests show no predictive power (AUC 0.49), and no profile has passed the out-of-sample gate that would allow "signal" wording. See [Screener](04-screener.md#scores-and-factors).
- **Descriptive (label)** — A score that describes where a company stands today (a ranking or a measure of unusualness) and makes no claim about future returns. The composite and factor scores carry it; only a recorded, passed out-of-sample gate replaces it with "Validated signal", and only for composites inside the population that gate tested. The gate counts only survivorship-free months (on or after the universe membership history start, 2026-10-10), so no pass is possible before about January 2029. See [Gate status of every model](16-methodology.md#gate-status-of-every-model).
- **Confluence zone** — A price area where several technical levels cluster (a peak of a weighted density of levels), with entry, invalidation and target. See [Technicals](05b-company-technicals.md#technicals).
- **Conformal prediction** — A method that turns a forecast into a set with guaranteed long-run coverage, using the empirical quantile of past forecast errors. See [Conformal prediction](16-methodology.md#conformal-prediction).
- **Context-free model** — The meta-labelling baseline that recalibrates T2 without any market or stock context. See [Meta-labelling](16-methodology.md#meta-labelling).
- **COT index** — Where a trader category's net position sits within its own range, `(net − min) / (max − min)` from 0 to 1, over the last 52 reports (1 year) or 156 reports (3 years). See [Positioning](02b-markets-cross-asset.md#positioning-cftc).
- **Coverage (conformal)** — The realised share of outcomes inside a set, compared with its target (for example 90%). See [Conformal validation](15-system.md#conformal-validation).
- **Coverage registry** — `contracts/coverage.yaml`: the list of datasets the app should hold, with cadence, entities and SLA. See [Data Health](15-system.md#data-health).
- **CQR (conformalized quantile regression)** — Conformal correction of a predicted quantile interval, using the score max(lo − y, y − hi), so coverage holds out of sample. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **Crowding** — The number of hedge-fund-like 13F filers holding a stock among their top-20 positions; model-based. See [Smart Money](07-smart-money-insiders.md#smart-money-overview).
- **CRPS (continuous ranked probability score)** — A proper score of a whole predictive distribution against the outcome, in the units of y; lower is better, and it reduces to the absolute error for a point forecast. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **CSCV (combinatorially symmetric cross-validation)** — The resampling scheme behind *PBO*. See [Multiple testing](16-methodology.md#multiple-testing).
- **Cycle clock** — A country's position in growth momentum × inflation momentum: recovery, expansion, slowdown or contraction. See [Macro](02a-macro.md#macro-cockpit).

## D

- **DAG** — A chain of dependent jobs run together (`nightly`, `weekly`, `monthly`, `cftc_weekly`, `hourly`). See [System overview](15-system.md#system-overview).
- **Data as of** — The latest market date in the serving snapshot, per region. See [Stale data](17-good-practice.md#stale-data).
- **DCF (discounted cash flow)** — Valuation as the present value of projected free cash flow to the firm, discounted at *WACC*, plus a terminal value. See [Company 360 fundamentals](05a-company-fundamentals.md#forecast).
- **Decile monotonicity** — How consistently mean returns rise (or fall) from the bottom to the top decile of a signal, measured by a Spearman correlation. See [Rank IC](16-methodology.md#rank-ic-and-factor-evaluation).
- **DML (double / debiased machine learning)** — Estimating a treatment effect while ML models absorb the controls. The models are cross-fitted and the score is orthogonal, so small errors in them do not bias the effect. See [Methods catalogue](09-model-studio.md#studio-v2-controls-causal-advanced-ml-and-neural-networks).
- **DQ (data quality)** — The result of the automatic data-quality checks on a load: ok, info, no data, warn or error. A blocking error stops publication. See [System overview](15-system.md#system-overview).
- **DSR (deflated Sharpe ratio)** — The probability that a strategy's Sharpe ratio beats the best Sharpe expected from the number of variants tried by chance alone. See [Multiple testing](16-methodology.md#multiple-testing).

## E

- **ECE (expected calibration error)** — The weighted average gap between predicted probability and realised frequency across bins; gate ≤ 2.5 pp for the technicals models. See [Calibration metrics](16-methodology.md#calibration-metrics).
- **Ellipsoid (conformal)** — A multi-dimensional conformal set shaped by a covariance matrix; minimum volume, but wide axis projections when coordinates are correlated. See [Ellipsoids versus boxes](16-methodology.md#ellipsoids-versus-boxes).
- **Embargo** — A gap after a test block from which no training data is taken, to stop leakage through overlapping labels. See [Purged and embargoed cross-validation](16-methodology.md#purged-and-embargoed-cross-validation).
- **E[R] (expected R)** — The model's expected outcome of a setup in *R* units, after costs, with unresolved trades marked to market. See [First-passage probabilities](16-methodology.md#first-passage-probabilities).
- **ERP (equity risk premium)** — The extra return demanded for equities over the risk-free rate, used in the cost of equity; by default backed out per region from current prices (market-calibrated). See [Company 360 fundamentals](05a-company-fundamentals.md#forecast).
- **ESEF** — The EU's European Single Electronic Format for annual reports (tagged iXBRL); the main official source of EU fundamentals. See [Data and coverage](01-data-and-coverage.md#company-filings-and-fundamentals).
- **ESS (effective sample size)** — The number of independent draws an autocorrelated MCMC sample is worth. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **EWMA** — Exponentially weighted moving average; the app's daily volatility estimate uses λ = 0.94 (RiskMetrics). See [Conformal prediction](16-methodology.md#split-conformal).
- **Experimental** — An output that failed its validation gate or was never validated; hidden unless **Show experimental outputs** is on, or available only in Model Studio / the Backtest Lab. See [Labels](17-good-practice.md#model-based-experimental-and-approximation-labels).

## F

- **Fan** — The calibrated band drawn for every horizon from 1 to 20 sessions on the Technicals chart (nested 80 / 90 / 95%). See [Marginal versus joint coverage](16-methodology.md#marginal-versus-joint-coverage).
- **FinBERT** — The English financial-sentiment language model used for news; *XLM-R* covers other languages. See [News & Sentiment](05a-company-fundamentals.md#news--sentiment).
- **First passage** — Which of two barriers (target or stop) the price reaches first within a horizon. See [First-passage probabilities](16-methodology.md#first-passage-probabilities).
- **FT-Transformer** — A neural-network model in the Model Lab; it failed the label-permutation leakage test and is not in production. See [Gate status](16-methodology.md#gate-status-of-every-model).
- **FTD (fails to deliver)** — Shares not delivered on settlement date, published by the SEC; an "FTD spike" is a model-based flag for unusually large fails. See [Smart Money](07-smart-money-insiders.md#smart-money-overview).

## G

- **Gap (data gap)** — Data the coverage registry expects but the lake lacks or holds in suspect form; classed missing, stale, partial, suspicious or unfillable. See [Data Health](15-system.md#data-health).
- **Gap filler** — The low-priority background process that repairs gaps and runs backfill campaigns under a resource governor; paused with the tray menu (`filler.PAUSE`). See [Data Health](15-system.md#data-health).
- **Gate (validation gate)** — A set of pass / fail criteria fixed before a model is evaluated (calibration, regimes, baselines, leakage, economics). See [Gate status of every model](16-methodology.md#gate-status-of-every-model).
- **Gaussian process (GP)** — A non-parametric Bayesian regression defined by a kernel; it gives a predictive mean and sd per point but costs O(n³), so the Studio caps n. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **GCN-GRU** — A graph convolutional recurrent neural network; the production filter of the peer-group conformal ellipsoids. See [Conformal validation](15-system.md#conformal-validation).
- **GDELT** — The Global Database of Events, Language and Tone: an open dataset of world news events and articles, updated every 15 minutes. See [Global Events](08-global-events-news.md#global-events).
- **Golden data checks** — A small set of stable or bounded facts (Apple revenue, the S&P 500 level, US CPI, ...) checked through the app's API after each refresh. See [QA guard](15-system.md#qa-guard).
- **GPR (geopolitical risk index)** — The app's model-based index of geopolitical risk per country from GDELT, compared with the published Caldara–Iacoviello index. See [Global Events](08-global-events-news.md#escalation--gpr).
- **Guide (Help page)** — This user guide inside the app at `/help` (menu **Guide**, command-bar `HELP`), with a chapter sidebar, full-text search and Print / PDF; the "?" icon in a page header opens the matching section. See [Getting started](00-getting-started.md#user-guide-help-page).

## H

- **hi52 (52-week-high proximity)** — Close divided by the highest close of the last 252 sessions; a validated "rank edge" factor. See [Technicals](05b-company-technicals.md#technicals).
- **HHI (Herfindahl–Hirschman index)** — The sum of squared shares; a concentration measure (holders, customers). See [Smart Money](07-smart-money-insiders.md#smart-money-overview).
- **HMM regime** — A 4-state hidden Markov model of the macro environment; model-based. See [Macro](02a-macro.md#macro-cockpit).
- **Holm correction** — A multiple-testing adjustment of p-values that controls the chance of any false positive. See [Multiple testing](16-methodology.md#multiple-testing).

## I

- **IC (information coefficient)** — The correlation between a signal and later returns across stocks; the app uses the rank (Spearman) version. See [Rank IC](16-methodology.md#rank-ic-and-factor-evaluation).
- **Implied path** — The policy-rate path implied by official yield or OIS curves; an approximation, not futures-implied probabilities. See [Macro](02a-macro.md#central-banks-overview).
- **IVOL (idiosyncratic volatility)** — The annualised standard deviation of a stock's returns not explained by its market and sector over 21 sessions; low IVOL is a validated "rank edge". See [Technicals](05b-company-technicals.md#technicals).

## J

- **Joint coverage** — The share of times all coordinates of a multi-dimensional set (several horizons, or high / low / close) are inside it at once. See [Marginal versus joint coverage](16-methodology.md#marginal-versus-joint-coverage).

## K

- **Kelly fraction (quarter-Kelly)** — The bet size that maximises long-run growth given win probability and payoff; the meta-labelling size uses a capped quarter of it (experimental). See [Meta-labelling](16-methodology.md#meta-labelling).
- **Knowledge date** — The date a fact became publicly known (filing, release, publication); the basis of point-in-time queries. See [Point-in-time data](16-methodology.md#point-in-time-data).

## L

- **LambdaRank** — A learning-to-rank objective (here LightGBM) that orders the rows of each query group, one group per date, to maximise NDCG. See [Methods catalogue](09-model-studio.md#studio-v2-controls-causal-advanced-ml-and-neural-networks).
- **Lazy Prices** — A flag for large year-over-year changes in the text of a company's 10-K risk factors. See [Company 360 fundamentals](05a-company-fundamentals.md#news--sentiment).
- **Level type** — A family of technical levels (pivots, period highs and lows, anchored VWAPs, volume nodes, ...), ranked per stock by how well it held historically. See [Technicals](05b-company-technicals.md#technicals).
- **Lint (Studio)** — The pre-run checks of a study for look-ahead, leakage, survivorship and tiny samples, each with a suggested fix. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **Log-loss** — The negative mean log-probability assigned to what actually happened; punishes confident errors. See [Calibration metrics](16-methodology.md#calibration-metrics).
- **Log-volume** — The normalised log of a conformal set's volume; favours ellipsoids. See [Ellipsoids versus boxes](16-methodology.md#ellipsoids-versus-boxes).

## M

- **Marginal coverage** — Coverage of one horizon (or coordinate) at a time, averaged over time and securities. See [Marginal versus joint coverage](16-methodology.md#marginal-versus-joint-coverage).
- **Market model** — The regression `r_stock = α + β·r_market + ε` estimated before an event to define the expected return. See [Event studies](16-methodology.md#event-studies-and-abnormal-returns).
- **MAX** — The largest daily return of the last 21 sessions; stocks with high MAX tend to do worse (validated "rank edge", negative sign). See [Technicals](05b-company-technicals.md#technicals).
- **Meta-labelling** — A second model that estimates whether a proposed trade will succeed given its context, and may decide take / skip and size. See [Meta-labelling](16-methodology.md#meta-labelling).
- **Mnemonic** — A Bloomberg-style command-bar function (`DES`, `GP`, `ECO`, `WIRP`, ...). See [BQL and mnemonics](14-bql-mnemonics.md#mnemonic-reference).
- **Moat** — A rating of durable competitive advantage (Wide, Narrow, None, Insufficient data) from evidence such as ROIC minus WACC and its persistence, margin stability and market share. See [Company 360 fundamentals](05a-company-fundamentals.md#moat).
- **Model Registry** — Named, versioned models promoted from Studio runs, with spec hash, data snapshot, owner, stage and drift monitoring. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **Model-based** — Our own estimate or transformation, not a published statistic or consensus. See [Model-based versus official data](16-methodology.md#model-based-versus-official-data).
- **mvp universes** — `mvp_us` (top 500 US companies by market cap) and `mvp_global` (curated global names); today's members, so history before 2026-10-10 is survivorship-biased. See [Survivorship](17-good-practice.md#survivorship).

## N

- **Newey–West t** — A t-statistic with standard errors robust to autocorrelation and heteroskedasticity, needed when horizons overlap. See [Rank IC](16-methodology.md#rank-ic-and-factor-evaluation).
- **Next-bar set** — The conformal set for tomorrow's high, low and close together; failed on sharpness, experimental. See [Conformal validation](15-system.md#conformal-validation).
- **NGBoost** — Natural-gradient boosting of the parameters of a predictive distribution (here a Normal's mean and scale). See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **Nowcast** — A model estimate of current-quarter GDP growth updated as monthly data arrive (US, euro area, UK, Japan), shown next to official nowcasts such as GDPNow. See [Macro](02a-macro.md#nowcast).

## O

- **OAS (option-adjusted spread)** — The yield spread of a bond index over Treasuries after adjusting for embedded options (ICE BofA indices). See [Licensing](17-good-practice.md#licensing).
- **Ops alert** — An operational warning (scheduler off, refresh overdue, data stale beyond SLA, regression guard failed, ...) shown on the System pages and as a Windows notification. See [System overview](15-system.md#system-overview).
- **Oster δ** — How strong selection on unobservables would have to be, relative to the included controls, to drive a coefficient to zero (Oster 2019); |δ| > 1 is the usual robustness threshold. See [Methods catalogue](09-model-studio.md#studio-v2-controls-causal-advanced-ml-and-neural-networks).

## P

- **Partial pooling** — Group-specific estimates shrunk toward the population mean in proportion to their noise (hierarchical model); between no pooling and complete pooling. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **PAV (pool-adjacent-violators)** — An algorithm that makes a sequence monotone with the least change; used to smooth the first-passage surface. See [First-passage probabilities](16-methodology.md#first-passage-probabilities).
- **PBO (probability of backtest overfitting)** — How often the best in-sample strategy variant ranks below the median out of sample (via *CSCV*). See [Multiple testing](16-methodology.md#multiple-testing).
- **PEAD (post-earnings-announcement drift)** — The tendency of prices to keep moving in the direction of an earnings surprise; the app's PEAD signal is model-based. See [Earnings & Events](05c-company-macro-events-model.md#earnings).
- **Peer group (conformal)** — A group of 20–50 related stocks (industry × region, or supply-chain / statistical ego groups) whose joint daily moves are tracked by the peer-group ellipsoid. See [Conformal validation](15-system.md#conformal-validation).
- **Pinball loss** — The loss minimised by a quantile forecast: τ·(y − q) above q and (1 − τ)·(q − y) below. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **PIT (point in time)** — Using at each past date only what was known on that date. See [Point-in-time data](16-methodology.md#point-in-time-data).
- **PIT (probability integral transform)** — The predictive CDF evaluated at the outcome; for a calibrated forecast the PIT values are uniform (a flat histogram). See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **PSI (population stability index)** — Σ (current − reference) · ln(current / reference) over reference deciles; 0.10 = watch, 0.25 = drift. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **PSR (probabilistic Sharpe ratio)** — The probability that the true Sharpe ratio exceeds a benchmark, given track-record length, skewness and kurtosis. See [Multiple testing](16-methodology.md#multiple-testing).
- **Purging** — Removing training rows whose label window overlaps the test period. See [Purged and embargoed cross-validation](16-methodology.md#purged-and-embargoed-cross-validation).

## R

- **R (risk unit)** — The distance from entry to the invalidation (stop); outcomes are measured in multiples of it. See [First-passage probabilities](16-methodology.md#first-passage-probabilities).
- **Rank edge** — The label of a technical factor that passed its rank-IC gate but is not profitable stand-alone after costs. See [Labels](17-good-practice.md#model-based-experimental-and-approximation-labels).
- **Rank IC** — The Spearman rank correlation between a signal and later returns across stocks, averaged over dates. See [Rank IC](16-methodology.md#rank-ic-and-factor-evaluation).
- **Recipe (Studio)** — A curated, ready-to-run study on real fields with a method note, expected outputs and its measured runtime. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **Regime gap** — Realised minus predicted frequency within a regime (Covid 2020, VIX > 25, ...); gate |gap| ≤ 5 pp. See [Calibration metrics](16-methodology.md#calibration-metrics).
- **Regression discontinuity (RD)** — An effect read from the jump in an outcome where a running variable crosses a cutoff. A fuzzy RD scales the jump by the jump in treatment take-up. See [Methods catalogue](09-model-studio.md#studio-v2-controls-causal-advanced-ml-and-neural-networks).
- **Regression guard** — The QA suites run after each refresh (API contract sweep, golden data checks, UI smoke). See [QA guard](15-system.md#qa-guard).
- **Reliability diagram** — A chart of predicted probability against realised frequency; a calibrated model lies on the diagonal. See [Calibration metrics](16-methodology.md#calibration-metrics).
- **R-hat (split)** — An MCMC convergence diagnostic comparing within- and between-chain variance; below 1.01 means the chains agree. See [Methods catalogue](09-model-studio.md#studio-v2-probabilistic-models-guided-builder-compare-and-registry).
- **Risk composite (risk-on / risk-off gauge)** — A model-based average of z-scored cross-asset components (equity trend, VIX, credit, copper / gold, AUD/JPY, USD); above +0.5 risk-on, below −0.5 risk-off. See [Cross-asset](02b-markets-cross-asset.md#cross-asset-dashboard).
- **R:R (reward-to-risk)** — Distance to the target divided by distance to the stop. See [Technicals](05b-company-technicals.md#technicals).
- **r-star (`r*`)** — The neutral real interest rate; the app shows official NY Fed estimates (Laubach–Williams, HLW). See [Macro](02a-macro.md#central-bank-page).

## S

- **Schedule profile** — The refresh plan: Weekly (default), Daily or Intraday (placeholder). See [System overview](15-system.md#system-overview).
- **Shrinkage** — Pulling a thin estimate toward a broader parent estimate in proportion to sample size. See [First-passage probabilities](16-methodology.md#first-passage-probabilities).
- **sigma_H (σ_H)** — The volatility over horizon H, `√V(H)`, from the EWMA daily variance plus an earnings jump term; distances to targets and stops are expressed in σ_H. See [First-passage probabilities](16-methodology.md#first-passage-probabilities).
- **SLA (freshness SLA)** — The maximum lag a dataset may have before it counts as stale (9 days under the Weekly profile, 3 under Daily). See [Data Health](15-system.md#data-health).
- **Smart cohort** — 13F hedge-fund-like filers whose past quarter-end books beat their peers; model-based. See [Smart Money](07-smart-money-insiders.md#smart-money-overview).
- **Smart-money score** — A model-based composite of 13F flows, smart-cohort flows, insider buying and short-interest signals; a descriptive screen with no significant IC. See [Smart Money](07-smart-money-insiders.md#smart-money-overview).
- **SMD (standardized mean difference)** — The difference in a covariate's mean between treated and control rows, divided by the pooled SD. |SMD| < 0.1 is read as balanced. See [Methods catalogue](09-model-studio.md#studio-v2-controls-causal-advanced-ml-and-neural-networks).
- **Snapshot (serving snapshot)** — The read-only database every page reads, rebuilt after each refresh and identified by a run id (for example `R20261022T2056130T13`). See [Citing the app's numbers](17-good-practice.md#citing-the-apps-numbers-in-research-notes).
- **Split conformal** — Calibrating a set's radius as a quantile of scores on data not used to fit the forecast. See [Split conformal](16-methodology.md#split-conformal).
- **Squeeze risk** — A model-based flag for high short interest (≥ 15% of shares, ≥ 5 days to cover) with a strong recent rise in a smaller company. See [Smart Money](07-smart-money-insiders.md#smart-money-overview).
- **Stacking** — Combining several models' predictions with a meta-learner fitted on out-of-fold predictions. See [Methods catalogue](09-model-studio.md#studio-v2-controls-causal-advanced-ml-and-neural-networks).
- **Statistical neighbours** — Companies whose returns co-move strongly; labelled statistical, not disclosed relationships. See [Company 360](05c-company-macro-events-model.md#network--geo).
- **Surprise (macro)** — Actual release minus the model-based expectation, divided by the historical forecast-error standard deviation. See [Macro](02a-macro.md#release-calendar).
- **Survivorship bias** — Flattering results from studying only companies that still exist today. See [Survivorship](17-good-practice.md#survivorship).

## T

- **T1** — The first target of a technical setup. See [Technicals](05b-company-technicals.md#technicals).
- **T2 / T3 / T4** — The Technicals v2 models: two-barrier first-passage probabilities (T2), zone hold given touch and its edge versus random (T3), and the calibrated technical score (T4). See [Gate status](16-methodology.md#gate-status-of-every-model).
- **Taylor rule** — A rule of thumb for the policy rate from the neutral rate, inflation's gap to target and the output gap; model-based with official inputs. See [Macro](02a-macro.md#central-bank-page).
- **Term premium** — The extra yield on long bonds over the expected path of short rates; the app uses the NY Fed ACM estimates. See [Macro](02a-macro.md#central-bank-page).
- **Touch probability** — The calibrated historical frequency of reaching a price level within h sessions. See [Touch probabilities](16-methodology.md#touch-probabilities).
- **Triple barrier** — A label with an upper barrier (target), a lower barrier (stop) and a time limit. See [Meta-labelling](16-methodology.md#meta-labelling).

## U

- **Unfillable** — A data gap the source cannot fill; recorded so nothing retries it. See [Data Health](15-system.md#data-health).
- **Universe** — A named list of securities (`mvp_us`, `mvp_global`, `eu_broad`, `full`, watchlists, screens). See [Data and coverage](01-data-and-coverage.md#universes).

## V

- **Valuation confidence** — A 0–100 measure of how reliable the fair value is; below 40 it is indicative only and excluded from scoring. See [Company 360 fundamentals](05a-company-fundamentals.md#forecast).
- **VIX** — The CBOE S&P 500 implied volatility index; VIX > 25 is one of the stress regimes in the validation gates. See [Calibration metrics](16-methodology.md#calibration-metrics).
- **Volume confirmation** — A breakout on high relative volume with a close near the day's extreme; confirmed breakouts followed through more often (validated edge, not a tradable one). See [Technicals](05b-company-technicals.md#technicals).

## W

- **w_proj** — The mean axis-projection width of a conformal set; the sharpness metric of the conformal gate. See [Ellipsoids versus boxes](16-methodology.md#ellipsoids-versus-boxes).
- **WACC (weighted average cost of capital)** — The blended cost of equity (risk-free rate + beta × ERP) and after-tax debt used to discount free cash flow. See [Company 360 fundamentals](05a-company-fundamentals.md#forecast).
- **Walk-forward** — Repeated fitting on the past and testing on the next unseen period, rolling forward through time. See [Purged and embargoed cross-validation](16-methodology.md#purged-and-embargoed-cross-validation).
- **WIRP** — The command-bar function for the central-bank monitor and implied path (`WIRP US`). See [BQL and mnemonics](14-bql-mnemonics.md#mnemonic-reference).

## X

- **XGBoost** — A gradient-boosted tree library; used for the production ML excess-return model and in meta-labelling. See [Model Lab](10-model-lab.md).
- **XLM-R** — A multilingual language model used for non-English news sentiment. See [News Terminal](08-global-events-news.md#news-terminal).

## Z

- **z-score** — A value's distance from a mean in standard deviations, for example against a company's own 5–10-year history or a country's 10-year history. See [Screener](04-screener.md#scores-and-factors).
