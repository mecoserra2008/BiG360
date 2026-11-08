# 16. Methodology appendix

This appendix explains the statistical methods that recur across the app, in one place, so that each page chapter can refer to them. It ends with a [summary table of the gate status of every model](#gate-status-of-every-model). The numbers quoted here come from the validation documents in `docs/` and from the gate files the jobs read (`data/reports/.../gate.json`); where a model has no formal gate, the table says so.

Two rules apply to everything below:

- **Model-based means ours.** Anything labelled model-based is a transformation or forecast made by the app, not a published statistic or a market consensus.
- **Calibrated over history, never "chance today".** A probability or a band that is "calibrated to 90%" was right 90% of the time on average over many past stocks and days. It is not a statement that the event has a 90% chance for this stock today.

---

## Point-in-time data

A backtest, a model or a historical screen is only honest if, at each past date D, it uses what was **known** at D. The app enforces this with a knowledge date on every versioned fact (`docs/ARCHITECTURE.md` §7).

**Knowledge dates by source**

| Source | Knowledge date |
|---|---|
| SEC filings | `filed` date (acceptance time stored as well) |
| EDINET / DART / CVM | submission / receipt date of the filing |
| ESEF | the filing's added or processed date (conservative), or its publication date |
| FRED macro | ALFRED `realtime_start` (the vintage date) |
| GDELT | `DATEADDED` |
| Form 4 insider trades | the filing date, never the transaction date |
| 13F holdings | the filing deadline (quarter end + 45 days, next SEC business day) + 1 business day |
| CFTC Commitments of Traders | the release (normally Friday 15:30 ET), not the Tuesday as-of date |

Where a source publishes no date (yfinance fundamentals, some AKShare data, macro without vintages), the knowledge date is **imputed** and flagged `knowledge_date_estimated = true`:

| Case | Lag after period end |
|---|---|
| US quarterly | +45 days |
| US annual | +75 days |
| Non-US quarterly / half-year | +60 days |
| Non-US annual | +120 days |
| Macro | the series' `release_lag_days` |

**Rules**

- **As-of query.** For a date D, take, per natural key, the row with the largest knowledge date ≤ D.
- **Restatements** are kept as new versions. A restatement of an older period never overwrites a newer one; a same-period restatement counts from its own filing date.
- **Labels.** Forward returns run from the close of D + 1 to the close of D + 1 + h, so information released after the close on D cannot leak into the label.
- **Prices.** A date D sees only bars dated ≤ D.

**Known deviations** (stated in the validation documents):

- **Sector classification is current only.** A company reclassified in the past appears in its current sector throughout.
- **Universe membership history starts on 2026-10-10.** Earlier, the mvp universes are today's names (survivorship bias). See [Good practice: survivorship](17-good-practice.md#survivorship).
- In the technicals and conformal validations, **past earnings dates are treated as if pre-announced**.
- W4 macro states in meta-labelling use today's vintages and parameters, lagged two months, not a true real-time history.

The BQL console exposes the same rules: `with(as_of=...)` sets the knowledge cut-off, and snapshot-only fields are refused with `dates=` or an earlier `as_of` (see [BQL and mnemonics](14-bql-mnemonics.md#point-in-time-rules)).

---

## Purged and embargoed cross-validation

Financial labels overlap in time: a 20-session forward return that starts on Monday shares 19 sessions with the one that starts on Tuesday. If a training row's label window overlaps the test period, ordinary cross-validation leaks the answer. The app uses **walk-forward** validation with **purging** and an **embargo** everywhere a model is fitted.

```
train  ─────────────────────┤purge├┤ test block ├┤embargo├─── next train
        rows whose label window ends inside the test block are removed (purge)
        rows just after the test block are skipped before training resumes (embargo)
```

| Model | Scheme |
|---|---|
| ML excess-return model (Model Lab) | Expanding window, minimum 5 years of training; training rows whose `label_end_date` ≥ test start are purged; embargo of 10 trading days after each test block. The production 3M run used 9 folds, 24-month test blocks, a 63-day purge and the 10-day embargo. |
| Technicals T2 first-passage surface | For test year Y, fitted on origin quarters < Q(Y) − 1 (a one-quarter purge). |
| Technicals T3 zone hold, v3 breakouts | 2-year test blocks; training origins before block start − 425 days; isotonic calibration on [start − 365, start − 60) days. |
| Meta-labelling | Fit block = origins before 1 Jan of Y − 1 minus an embargo of 1.5 H sessions in calendar days + 7 (28 / 49 / 91 days for H = 10 / 20 / 40); inner validation on the fit block's last year; calibration and threshold on [1 Jan Y − 1, 1 Jan Y − embargo). Sample weights = average uniqueness per security. |
| Conformal sets | Filters fitted on data before 1 Jan of Y − 1 and then frozen; calibration on the last 500 realised scores from origins on or after 1 Jan Y − 1. |
| v3 technical factors | No fitted parameter: definitions and signs pre-registered from the literature, so the whole sample is out of sample. The composite is built walk-forward (factors selected on data up to 30 days before 1 Jan of Y). |

**Leakage tests** used by the gates:

- **Label permutation:** refit on shuffled labels; out-of-sample skill must vanish. The FT-Transformer failed this test (see the gate table).
- **Causal invariance:** replace every bar after a cut with noise; every feature before the cut must be bit-for-bit unchanged.
- **Peeking control:** a deliberately leaky feature must be caught.
- **Shift-by-one:** the factor dated one session earlier must keep most of its information content.

---

## Conformal prediction

Conformal prediction turns any forecast of scale or shape into a set (a band, a box or an ellipsoid) with a guaranteed long-run **coverage**: the share of times the realised outcome falls inside the set. The app's implementation follows Limmer, *Filtered Conformal Ellipsoids for Graph-Native Time Series* (`docs/CONFORMAL_VALIDATION.md`).

### Split conformal

1. A **filter** produces, before the outcome is known, a centre and a scale or covariance (for prices: an EWMA variance, λ = 0.94, plus an earnings jump term when an earnings day falls inside the horizon).
2. Each past outcome gets a **nonconformity score**, for example the squared standardised return of horizon h:

```
R_t = y_h² / V(h)          V(h) = h·s²_t (+ J_t if an earnings day falls in (t, t+h])
```

3. The **radius** is the empirical (1 − α) quantile of the most recent calibration scores:

```
q̂ = the ⌈(n + 1)(1 − α)⌉-th smallest of the last n = 500 realised scores
set: { y : R(y) ≤ q̂ }      → band  ±√(q̂ · V(h))  around the centre
```

If the scores are exchangeable, `P(outcome in set) ≥ 1 − α` on average. Because scores are only known once the outcome is realised, the calibration window uses a lag equal to the horizon (20 sessions for a 20-session band).

### Pooled calibration

A single stock has too few scores for a stable 90% or 95% quantile. Scores are therefore **pooled** across the securities of a **region × sector** (separate pools for windows containing an earnings event), re-sorted every 5 sessions. Each pool holds at least 30 securities × 500 origins. The resulting coverage is a property of the pool, not of the individual stock.

### Adaptive conformal inference (ACI)

ACI (Gibbs & Candès, 2021) adjusts the working level online after each miss:

```
α_{t+1} = α_t + γ · (α − err_t)        err_t = 1{ R_t > q̂(α_t) }        γ = 0.005, restarted each year
```

Long-run average miscoverage converges to α for any score sequence. The app uses delayed feedback (the error of a 20-session set is known 20 sessions later) and pooled feedback (err_t is the share of the pool that missed). When α_t ≤ 0 the set is **unbounded**; such sets count as covered but are left out of width statistics. ACI is used in production for the next-bar sets and the peer-group sets, where one-session feedback lets it react; the 20-session bands use the plain radius.

### Ellipsoids versus boxes

For several coordinates at once (the 1, 5, 10 and 20-session returns; tomorrow's high, low and close; the returns of 20–50 peer-group members) the set can be:

- an **ellipsoid**, `{ y : (y − μ)ᵀ Σ⁻¹ (y − μ) ≤ q̂ }`, the minimum-volume set for a given covariance; or
- a **box**: one interval per coordinate, each calibrated so that all hold together (for example a Bonferroni box with α/N per coordinate, or a conformally calibrated ATR box).

Two size metrics compare them (paper eq. 3–4):

| Metric | Definition | Favours |
|---|---|---|
| **w_proj** | mean coordinate-projection length (2 × half-width of the set's shadow on each axis), in log-return units, averaged over coordinates | boxes, when coordinates are strongly correlated |
| **log-volume** | normalised log-volume of the set (for boxes, the mean log of the side lengths) | ellipsoids (minimum volume) |

The gate was evaluated on w_proj, as specified, and both are reported. The joint price envelope had the smallest log-volume of all calibrated sets but projections 35% wider than a calibrated ATR box, because overlapping horizons are highly correlated (ρ(Y10, Y20) ≈ 0.7) and the minimum-volume ellipsoid is long along the diagonal. That is why it failed on sharpness.

### Marginal versus joint coverage

- **Marginal (per-horizon) coverage:** each band is calibrated alone; the 20-session band covers the 20-session outcome 90% of the time. This is what the Technicals chart shows (the **calibrated range** and the fan, h = 1..20; per-horizon coverage 89.6–90.0% at 90%).
- **Joint (simultaneous) coverage:** all coordinates inside the set at once. Four separately calibrated 90% bands do **not** cover jointly 90% of the time: in the walk-forward audit the joint coverage of the four marginal bands is 73.0%. Conversely, the projections of a joint 90% ellipsoid cover each horizon about 98.7% of the time, so they are simultaneous bands, not 90% bands.

Coverage in every case is **marginal over time and securities**. Per-regime coverage is reported, not guaranteed: in the Covid window (origins 15 Feb – 30 Jun 2020) the 20-session sets of every method covered only about 77–78% at 90%, because the shock arrived after the origins and a 20-session lag gives no feedback in time.

### Touch probabilities

The probability that the price **touches** a level within h sessions is read from calibrated curves rather than a formula. For each pool, the share of past origins whose standardised running high (or low) reached a standardised distance

```
u = | log(level / close) | / √V(h)
```

is stored per horizon. Validated horizons are 1, 5, 10 and 20 sessions (predicted equals realised within 0.2 pp, US mvp 2011–2026). Longer horizons (21–252 sessions) reuse the 20-session curve with the variance scaled by h and are flagged extrapolated, **not validated**. A Gaussian reflection-principle formula is about 5× too low in the tail (P(u = 3) = 0.0027 against a realised 0.014).

---

## First-passage probabilities

The Trade setup card asks: starting from today's close, which comes first within H sessions, the target (T1) or the invalidation (stop)? This is a **two-barrier first-passage** problem (`docs/TECHNICALS_V2_VALIDATION.md`, T2).

### Event definition

- Barriers: T1 above the close and the invalidation below it (mirrored for a short).
- Over the next H sessions, the **high** reaching T1 is a win, the **low** reaching the invalidation is a loss; a bar that touches both counts as a **loss** (intrabar order unknown); otherwise **neither** by H.
- Distances are standardised by the horizon volatility:

```
a = log(T1 / close) / σ_H        b = log(close / invalidation) / σ_H        σ_H = √V(H)
```

### Grid estimator (production)

1. For every daily origin of the gate universe (3.2 million origins × 5 horizons), record the outcome of **every** (a, b) pair on a 20 × 20 logarithmic grid from 0.1 to 8 σ_H. This uses all paths, not only the origins that happened to have a setup.
2. Count wins, losses and neither per cell, per pool: **region × volatility regime × earnings window**.
3. **Shrinkage.** Thin cells borrow strength from the parent pool, pool → region → all regions, with m = 500 pseudo-origins:

```
p̂_cell = (n_cell · p_cell + m · p_parent) / (n_cell + m)
```

4. **Monotone smoothing (PAV).** P(win) must fall as T1 moves away and rise as the stop moves away (and the reverse for P(loss)). Iterated pool-adjacent-violators passes along both grid axes enforce this.
5. **Interpolation.** Bilinear in (log a, log b) between grid nodes.
6. For each cell the mean terminal move of the "neither" paths is kept, so the **expected R** can mark unresolved trades to market:

```
E[R] ≈ P(win) · R:R − P(loss) · 1 + P(neither) · (mean move of "neither" paths, in R) − costs (in R)
```

Costs are the Backtest Lab's (commission 1 bp, ADV-bucket half spread, square-root impact on a $200k position, borrow for shorts).

**Baselines** it must beat: the driftless **Brownian** two-barrier formula (too pessimistic on P(win) at H ≤ 10 by 1.2–2.7 pp) and the naive **R:R-only** rule (badly miscalibrated at short horizons: ECE 15.8 pp at H = 5, because it ignores "neither by H"). A gradient-boosted candidate was better calibrated at 40 and 60 sessions but worse on Brier at 5–20 and still worse than Brownian at 40 / 60, so it was not deployed. On simulated Brownian paths the grid estimator matches the analytic formula within 1.5 pp.

### Verdict in short

Only **H = 20** passed the full gate and is shown by default. H = 5 and 10 failed on the Covid 2020 window (5.1–5.6 pp more stop-outs than predicted); H = 40 and 60 failed because the grid no longer beats the Brownian baseline. The economic check of filtering setups by E[R] > 0 was a **null result** (+1.0%/yr at H = 20, t = 1.33).

---

## Meta-labelling

Meta-labelling (Lopez de Prado, *Advances in Financial Machine Learning*, ch. 3–4) separates **direction** from **sizing**. A primary model proposes a trade; a secondary (meta) model estimates how likely that trade is to succeed given the context, and can decide to take or skip it and how much to risk (`docs/TECHNICALS_V3_VALIDATION.md`, "Meta-labelling (#2)").

- **Primary model:** the existing v2 setup generator (direction, entry, T1, invalidation) with its T2 probabilities.
- **Label (triple barrier):** win = T1 before the invalidation within H sessions; loss = invalidation first (same-bar touch of both = loss); vertical barrier at H (20 primary; 10 and 40 secondary).
- **Features (point in time, 89):** T2's own probabilities and E[R]; geometry in σ units; setup sub-scores; technical context (squeeze percentile, ADX, trend alignment, ...); market context (VIX level and change, cross-asset risk gauge, macro regime states lagged two months); peer-group break-aways; insider, short-interest and smart-money signals; earnings proximity; region / sector; the v3 factors and breakouts.
- **Learners:** logistic regression and XGBoost, both using T2's logit as an **offset** (base margin), so a model that finds no usable context reproduces T2. Calibration by identity, Platt or isotonic on a held-out year. A take / skip threshold and a capped quarter-Kelly size are chosen in sample.
- **Baselines:** T2 alone, and a **context-free** logistic recalibration of T2 (T2 + geometry + setup descriptors, no context).

**Verdict.** H = 10 **passed**: better calibrated than T2 (ECE 0.50 / 0.78 pp for win / loss; Covid loss gap +2.5 pp against T2's +5.1 pp) with a Brier improvement over T2 of −0.00040 [−0.00071, −0.00006]. But it is **not** significantly better than the context-free model (−0.00022 [−0.00050, +0.00013]). The pass therefore means "a better-calibrated P(win) than T2", not "context has a demonstrated edge". H = 20 and 40 failed, and the **take / skip and size decisions failed the economic check** (US meta-filtered minus all setups −4.3%/yr, t = −2.55 at H = 20).

---

## Calibration metrics

A probability model is **calibrated** if, among all cases where it said 30%, the event happened about 30% of the time. The gates measure this in several ways.

**Expected calibration error (ECE).** Sort predictions into B bins (15 equal-count bins in the technicals gates), then

```
ECE = Σ_b (n_b / N) · | mean predicted p in bin b − realised frequency in bin b |
```

Reported in percentage points. Gate thresholds: ECE ≤ 2.5 pp (technicals v2, v3 and meta-labelling).

**Brier score.** Mean squared error of the probabilities; for K outcomes (win / loss / neither):

```
Brier = (1/N) Σ_i Σ_k (p_ik − y_ik)²          lower is better; compared with baselines
```

**Log-loss.** `−(1/N) Σ_i log p_i,observed`. Punishes confident mistakes heavily. Gates require the model to beat its baselines on both Brier and log-loss, usually with a month-block bootstrap confidence interval of the difference that excludes zero.

**Reliability diagram.** Predicted probability (x) against realised frequency (y) per bin; a calibrated model lies on the diagonal. The validation runs write them as `reliability_*.svg` under `data/reports/`.

**Regime gaps (calibration in the large).** Within a regime (Covid 2020 = origins 15 Feb – 30 Jun 2020, the year 2020, 2022, VIX > 25, each region, side, volatility regime, earnings window), the gap is

```
gap = realised frequency − mean predicted probability        gate: |gap| ≤ 5 pp for every regime with n ≥ 2,000
```

Overall calibration can hide a regime failure: T3 P(hold | touch) has ECE 1.45 pp overall but a −8.7 pp gap in Covid 2020, and failed.

**Coverage** (for conformal sets) is the analogue for intervals: realised share inside the set against the target; the conformal gate allowed ±1.5 pp overall and no regime more than 5 pp under.

---

## Multiple testing

When many hypotheses or strategy variants are tried, some look significant by chance. The validation documents correct for this, and the Backtest Lab reports the same statistics for your own strategies (see [Backtest Lab](11-backtest-lab.md)).

**Holm (family-wise error).** Sort the m p-values ascending, p₍₁₎ ≤ … ≤ p₍ₘ₎; the adjusted values are

```
p̃₍ᵢ₎ = max_{j ≤ i} min(1, (m − j + 1) · p₍ⱼ₎)
```

Used for the per-factor verdict of the v3 technical factors over the 7 primary cells (hi52 0.017, strev 0.002, MAX 9e-10, IVOL 6e-13 passed; mom 0.30, rmom 0.56, indmom 0.56 did not).

**Benjamini–Hochberg (false discovery rate).** Controls the expected share of false discoveries:

```
q₍ᵢ₎ = min_{j ≥ i} min(1, m · p₍ⱼ₎ / j)
```

Over the 105 v3 factor cells, 49 were significant at q = 0.05, every one with the literature's sign. BH is also applied to Model Studio result tables.

**Probabilistic Sharpe ratio (PSR).** The probability that the true Sharpe ratio exceeds a benchmark SR*, given track-record length n and the skewness γ₃ and kurtosis γ₄ of returns (Bailey & Lopez de Prado, 2012):

```
PSR(SR*) = Φ( (SR − SR*) · √(n − 1) / √(1 − γ₃·SR + (γ₄ − 1)/4 · SR²) )        (per-period SRs)
```

**Deflated Sharpe ratio (DSR).** PSR against the Sharpe ratio expected from the **best of N unskilled trials** (Bailey & Lopez de Prado, 2014):

```
SR* = E[max SR] = √V[SR_trials] · ( (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) )        γ ≈ 0.5772 (Euler–Mascheroni)
DSR = PSR(SR*)
```

When the trial Sharpe ratios hardly differ, V[SR] is small and DSR stays near 1 for every configuration; the validations note that DSR "does not discriminate" in that case (about 0.99 in the conformal and v2 economic checks).

**Probability of backtest overfitting (PBO) via CSCV.** Combinatorially symmetric cross-validation (Bailey, Borwein, Lopez de Prado & Zhu, 2017) with S = 10 time blocks:

1. For every split of the blocks into two halves (C(10, 5) = 252 combinations), pick the configuration with the best in-sample Sharpe.
2. Find its relative rank w among all configurations out of sample, and its logit `λ = ln(w / (1 − w))`.
3. `PBO = share of splits with λ ≤ 0`, i.e. how often the in-sample winner is below the out-of-sample median.

PBO near 0 means the in-sample choice holds up; near 0.5 or above means the selection is no better than chance. Examples: conformal breakout configurations 0.54; v2 E[R] configurations 0.07; meta-labelling 0.35 over 62 configurations.

---

## Rank IC and factor evaluation

A **rank information coefficient (IC)** measures whether a signal orders stocks correctly, without assuming a linear relation.

```
IC_t = Spearman( signal_i,t , forward return_i,t→t+h )        across stocks i on date t
mean IC = average of IC_t over dates
```

| Element | How the app does it |
|---|---|
| **Labels** | Sector-neutral: the forward log return from the close of t + 1 minus the equal-weight mean of liquid names in the same region × sector × date (region × date for thin or unclassified sectors). ML labels are cross-sectional ranks of 1M / 3M / 6M forward excess return vs sector × region. |
| **Signal** | The within-sector percentile of the factor (0..100). |
| **Pooling** | Per (region, date) Spearman, pooled per date weighting regions by number of names; weekly origins (1,216 dates for the v3 factors). |
| **Newey–West t** | The t-statistic of the mean IC with HAC standard errors, because overlapping horizons make IC_t autocorrelated: ceil(h / 5) + 1 lags for weekly origins. Non-overlapping samples are used where stated. |
| **Decile monotonicity** | Spearman correlation between decile number and decile mean return (sign-aligned); the v3 gate requires ≥ 0.7 and a top-minus-bottom spread whose 95% month-block CI is above 0. |
| **Robustness** | Aligned in ≥ 3 of 4 regions with none significantly wrong (BH), aligned in ≥ 60% of years, no regime with aligned t ≤ −2, shift-by-one keeps ≥ 50% of the IC, label-permutation null. |
| **Economics** | Reported separately: US long-short net Sharpe after costs and long-only quintile vs equal-weight universe. |

**Reading an IC.** A rank IC of 0.02–0.05 is typical for useful equity signals but small: it is a median, cross-sectional effect, and it need not survive costs. The passing v3 factors (hi52 +0.023, MAX −0.038, IVOL −0.044 at 20 sessions) are **not profitable as stand-alone strategies after costs**; their simple-return decile spreads are much smaller than their log-return spreads because the high-volatility deciles carry variance drag. They are labelled "rank edge" in the app.

The ML model's production promotion rule is out-of-sample rank IC t > 2 and beating the incumbent (`docs/ARCHITECTURE.md` §11).

---

## Event studies and abnormal returns

Event studies measure the price reaction to an event (earnings, a geopolitical shock, a CFTC extreme) relative to what the market alone would predict.

**Market model.** Estimated by OLS on trading days [−250, −30] relative to the event day 0:

```
r_i,t = α_i + β_i · r_m,t + ε_i,t
AR_i,t = r_i,t − (α̂_i + β̂_i · r_m,t)                       abnormal return
CAR_i[t1, t2] = Σ_{t=t1..t2} AR_i,t                           cumulative abnormal return
BHAR_i[t1, t2] = Π(1 + r_i,t) − Π(1 + α̂_i + β̂_i · r_m,t)      buy-and-hold abnormal return
```

| Use | Benchmark r_m | Windows | Notes |
|---|---|---|---|
| Earnings reactions (Company 360 › Earnings, Earnings season) | the issuer's country index (ACWI fallback), on the security's own trading days | [−1, +1], [0, +5], [+1, +20] | At least 60 valid estimation days; daily moves above 50% treated as data errors. "Move vs normal" = the [−1, +1] move against residual σ × √3. Day 0 = first trading day on or after the reaction date. |
| Event-study library (Global Events) | country index vs ACWI; sector SPDR vs SPY; issuer vs its country index | [0, 1], [0, 5], [−1, 5], [0, 10] | Shocks: attention z ≥ 2.5 or tone z ≤ −2.5 with at least 20 items, no shock of the same kind in the previous 10 days. "Expected reaction" = the historical mean CAR of similar labelled shocks (model-based). |
| Model Studio `event_car` method | user choice (market model or constant mean) | user choice | Reports AAR / CAAR paths, cross-sectional t and Patell Z. |

**Significance.** The cross-sectional t uses the dispersion of CARs across events; the Patell Z standardises each AR by its estimation-period residual σ (with the forecast-error adjustment).

**Pitfalls.** CAR sums simple abnormal returns and understates compounding over long windows; BHAR compounds but is skewed. Overlapping events in the same market are not independent. Earnings reactions use the official release time where available, otherwise the provider date or the filing date (labelled), so a wrong date shifts day 0.

---

## Model-based versus official data

The macro data policy of the app (`docs/MACRO_V2_PLAN.md`, `docs/MACRO_V3_PLAN.md`):

1. **Inputs come from official sources only:** central banks, statistical offices, treasuries, regulators, the NY Fed and other Fed banks, OECD, BIS, IMF, Eurostat. FRED and ALFRED count as official (St. Louis Fed). No Yahoo or scraped commercial sites for macro. (Market prices for equities, futures and ETFs are market data, not macro.)
2. **Expectations are model-based** and labelled everywhere they appear: "Model-based expectation (not consensus)". There is no consensus feed. Surprises are measured against these expectations.
3. **Approximations are acceptable but labelled**, with their method and caveats (for example the central-bank implied path).

How the labels read in the app:

| Output | Label (as served by the API) |
|---|---|
| Release expectations | "Model-based expectation (not consensus)" |
| Surprise indices | Economic surprise indices vs model-based expectations (not consensus) |
| Central-bank implied path | "Approximation from official curves (not futures/OIS-implied probabilities)" |
| Term-premium-adjusted path | "Term-premium-adjusted with NY Fed ACM term premia (model-based, official NY Fed estimates)" |
| Taylor rule | "Model-based (Taylor rule; official inputs)" |
| Expected inflation | "Model-based (Cleveland Fed model for the US; our AR(1) elsewhere)" |
| Central-bank tone | "Model-based (local RoBERTa hawkish/dovish classifier + lexicon)" |
| Rate cycle | "Rule-based: most recent policy-rate change within 183 days decides hiking/cutting, else pausing" |
| Recession panel | each method labelled: NY Fed yield-curve probability (official, published); Sahm rule (official); our probit and HMM P(contraction) (model-based, ours) |
| Macro betas | "Model-based: rolling weekly OLS betas (156 weeks, >= 52), Newey-West HAC(4) standard errors" |

The same principle extends beyond macro: earnings expectations are model-based (a seasonal time-series model on the company's own history, 80% band), not consensus; the smart-money definitions (hedge-fund-like filers, smart cohort, crowding, squeeze risk, FTD spike, the combined score) are model-based; supply-chain statistical neighbours are labelled statistical and never shown as disclosed relationships; the cross-asset risk composite and trend signals are MODEL-BASED transformations of market and official data.

---

## Gate status of every model

One row per model or output. **Gate** quotes the verdict and its key numbers from the source document; "no formal gate" means no pre-specified pass / fail test exists, and the row says how the app labels the output instead. "Experimental" outputs are hidden unless **Show experimental outputs** is on (Technicals tab) or appear only in Model Studio / the Backtest Lab.

### Technicals and conformal sets

| Model / output | Where shown | Gate verdict and key numbers | Label in the app | Source |
|---|---|---|---|---|
| Joint 1/5/10/20-session conformal envelope (ellipsoid) | Not in the main UI; Model Studio, Backtest Lab | **FAIL**: coverage 89.6 / 94.5% but Covid 2020 77–78% at 90%; w_proj +35% vs a calibrated ATR box | experimental; "not shown" on the validation page | CONFORMAL_VALIDATION Verdict, A.1 |
| Next-bar high / low / close set | Model Studio, Backtest Lab | **FAIL (sharpness)**: coverage 90.0 / 95.0%, every regime on target with ACI; w_proj +43% vs calibrated ATR box | experimental | CONFORMAL_VALIDATION A.3 |
| Marginal single-horizon bands (qm_*) and fan h = 1..20 | Technicals chart (calibrated range, fan), BQL `cf.*` | **PASS (coverage)**: 89.6–90.0% at 90% per horizon; ~40% narrower than 2×ATR (which covers 98%); not sharper than a calibrated ATR band | "calibrated band", model-based; "calibrated to 90% over history" | CONFORMAL_VALIDATION A.2, A.5 |
| Touch probabilities, h = 1 / 5 / 10 / 20 | Technicals (chance of touch), thesis target / stop | **PASS**: predicted = realised within 0.2 pp at every distance and horizon | model-based, calibrated | CONFORMAL_VALIDATION A.4 |
| Touch probabilities, h = 21–252 | Thesis tracker, API | not validated (20-session curve with variance scaled by h) | `extrapolated = true`, NOT validated | api.yaml (conformal touch) |
| Peer-group ellipsoid (GCN-GRU + ACI) | Peers tab conformal panel, RV | **PASS** as a calibrated descriptive measure: 90.4 / 95.2%; worst regime Covid 85.3% at 90%; −10% w_proj vs EWMA | anomaly percentile, pair / basket bands, model-based; **not** a trading signal | CONFORMAL_VALIDATION B, Verdict |
| Band breakout signal | Backtest Lab | **FAIL**: not better than a naive N-day high (paired t −0.43 to −3.23) | experimental signal | CONFORMAL_VALIDATION Economic (i) |
| Calibrated band stops | Backtest Lab | **MIXED**: beat ATR stops (+6.2%/yr, t 3.56) but worse than no stop (−2.2%/yr, t −2.64) | experimental stop / target mode | CONFORMAL_VALIDATION Economic (ii) |
| Peer break-away fade | Backtest Lab | **FAIL (null)**: Sharpe −0.09 / −0.15; vs naive fade t ≤ 1.17 | experimental signal | CONFORMAL_VALIDATION Economic (iii) |
| Level-type ranking (confluence zones) | Technicals levels / zones | no formal gate; out of sample, top-ranked level types beat random levels by about 1.5 pp bounce rate | entry filter, not a stand-alone strategy | USER_GUIDE §3 |
| T2 P(win / loss), H = 20 | Trade setup card | **PASS**: ECE 1.13 / 1.47 pp; worst regime Covid win −4.6 / loss +4.0 pp; Brier 0.3741 vs Brownian 0.3759 / R:R-only 0.3834 | production, model-based, "calibrated on history" | TECHNICALS_V2 Verdict |
| T2, H = 5 and 10 | Trade setup card (toggle) | **FAIL**: Covid 2020 loss gap +5.6 / +5.1 pp | experimental (hidden by default) | TECHNICALS_V2 Verdict |
| T2, H = 40 and 60 | Trade setup card (toggle) | **FAIL**: no better than Brownian (Brier 0.3485 vs 0.3482; 0.3453 vs 0.3429); H = 60 ECE 2.68 / 2.73 pp | experimental (hidden by default) | TECHNICALS_V2 Verdict |
| Expected R filter (E[R] > 0) | Trade setup card, Backtest Lab | economic check **null**: +1.0%/yr at H = 20 (t 1.33), −0.6%/yr at H = 10 (t −0.75) | E[R] shown; no edge claimed | TECHNICALS_V2 Economic check |
| T3 P(hold given touch) | Zones (toggle) | **FAIL**: ECE 1.45 pp but Covid 2020 −8.7 pp | experimental | TECHNICALS_V2 Verdict, T3 |
| T3 per-zone edge vs random | Zone flag | **PASS**: bucket predicted vs realised within 1.9 pp; level features beat the random control (Brier −0.00045 [−0.00066, −0.00025]); on average touched zones hold −0.63 pp **less** often than random bands | "holds more / less often than random" or "no reliable edge" | TECHNICALS_V2 Verdict, T3 |
| T4 calibrated technical score → outcomes | Technical score card (toggle) | **FAIL**: rank IC −0.003 / −0.012 / −0.014 at 5 / 20 / 60 d, n.s. | experimental; card says "no out-of-sample edge" | TECHNICALS_V2 Verdict, T4 |
| v3 factors hi52, MAX, IVOL | Technicals factors, BQL `tech.*`, Backtest Lab | **PASS**: 20-day IC +0.023 (t 2.9), −0.038 (t −6.4), −0.044 (t −7.5); none profitable stand-alone after costs | "rank edge" with economic note, shown by default | TECHNICALS_V3 Verdict #1 |
| v3 factors mom, rmom, strev, indmom | Technicals factors (toggle) | **FAIL**: mom, rmom, indmom fail G1 (Holm p 0.30 / 0.56 / 0.56); strev passes G1 (0.002) but fails monotonicity (0.35) | experimental | TECHNICALS_V3 Verdict #1 |
| v3 factors outside US / EU / UK / JP | Technicals factors | not validated | "region not validated" | TECHNICALS_V3 Production configuration |
| v3 composite factor score | not served | **FAIL**: G6 vs the v2 score (t 1.66) and G7 economics (US L/S net Sharpe −0.46) | no composite value served | TECHNICALS_V3 Verdict #1 |
| Calibrated P(follow-through), H = 10 / 20 | Breakouts (toggle) | **FAIL**: ECE 1.23 / 1.20 pp but Covid 2020 +18.3 / +17.2 pp | experimental | TECHNICALS_V3 Verdict #5 |
| Volume-confirmation edge (state chip, flag, score) | Breakouts | **PASS**: geometry-adjusted +1.6 pp [+1.2, +2.0]; vs matched control +3.5 pp [+2.7, +4.6]; no tradable edge (confirmed vs random subset −1.0%/yr, t −0.6) | deterministic chip and flag shown | TECHNICALS_V3 Verdict #5 |
| Meta-labelled P(win), H = 10 | Trade setup "Context-adjusted (meta)" row, thesis meta table | **PASS**: ECE 0.50 / 0.78 pp; Brier vs T2 −0.00040 [−0.00071, −0.00006]; not better than context-free (−0.00022, CI includes 0) | default row, model-based | TECHNICALS_V3 Meta-labelling Verdict |
| Meta P(win), H = 20 and 40 | Same (toggle) | **FAIL**: not better than T2 / context-free | experimental | TECHNICALS_V3 Meta-labelling Verdict |
| Meta take / skip and size | Same (toggle), Backtest Lab `meta_setups` | **FAIL** (economic): US −4.3%/yr (t −2.55) at H = 20 | experimental at every horizon | TECHNICALS_V3 Meta-labelling Economics |
| Meta outputs outside the validated domain (ETFs, indices, regions other than US / EU / UK / JP) | Trade setup card | not validated | always experimental | TECHNICALS_V3 Meta-labelling Production configuration |

### Scores, forecasts and alternative data

| Model / output | Where shown | Gate verdict and key numbers | Label in the app | Source |
|---|---|---|---|---|
| ML excess-return model (XGBoost, 1M / 3M / 6M) | Company 360 › Model, Model Lab, composite ML blend profile | no formal pass / fail beyond the promotion rule (OOS rank IC t > 2 and beat the incumbent); OOS rank IC about 0.035 (production runs: 1M 0.0352, t 11.7; 3M 0.0349, t 12.2; 6M 0.0212, t 5.7) | "prod" badge in Model Lab; one input, not proven alpha; backtests before 2026-10-10 survivorship-biased | USER_GUIDE §3; ARCHITECTURE §11; `/models/runs` |
| FT-Transformer (3M and 1M) and the ensemble | Model Lab | **FAIL** label-permutation leakage test (IC +0.06–0.09 on shuffled labels) → demoted; not in production. Its high Model Lab rank IC (0.126 for 1M) is not evidence of skill | shown without a "prod" badge; no leakage note in the UI | PHASE3_TODO (ML leakage audit) |
| Insider XGBoost (P(3M excess > 0)) | not used | no out-of-sample edge (latest runs: 3M rank IC −0.015, t −1.17; 1M −0.0005) | only the rules-based insider score is used | USER_GUIDE §6; `/models/runs` |
| Insider signal score and anomaly score (rules + IsolationForest) | Insiders tab, Insider Radar, Insider factor | no formal gate | descriptive score 0–100 (50 = no activity) | ARCHITECTURE §11 |
| Composite score, 10 factor scores, sector KPI scores | Overview, Screener, Sector Explorer, Peers | **not passed** (no out-of-sample edge): Model Studio factor scores → 3M outperformance AUC 0.490 (`SR20261012T0319222S5ZX`, purged 5-fold, n 42,384); gate `composite_validation` (rank IC and AUC, Newey-West t ≥ 2 over ≥ 24 PIT scoring months, one date per month; full runs only; panel masked by point-in-time universe membership and only months on or after the membership history start, 2026-10-10, can decide a pass, so survivor-only history never validates; a pass applies only to the tested securities and score scope) not passed by any profile (2024 check: rank IC −0.003, t −0.18; AUC 0.502, t 0.24) | **Descriptive** badge; API `label: descriptive`; "signal" / predictive / buy-sell wording only behind a passed gate; score 0–100, grade A–E; 50 = neutral or no data | ARCHITECTURE §11; `contracts/scoring.yaml` composite.validation; coverage v3 A8 |
| Forecast, DCF and blended fair value | Forecast tab, BQL `pe_fwd` | no formal gate | model-based, not consensus; valuation confidence < 40 = indicative only and excluded from scoring | USER_GUIDE §3 |
| Moat rating | Moat tab | no formal gate (rule-based evidence components) | Wide / Narrow / None / Insufficient data | USER_GUIDE §3 |
| Beneish M-score screen | Company 360 alerts | no formal gate | "MODEL-BASED, not an accounting finding" (recalibrated screen) | api.yaml; AUDIT F15 |
| Earnings expectations and surprises | Earnings tab, Earnings season, calendar | no formal gate (seasonal model chosen by trailing MAPE; 80% band) | model-based, not consensus | USER_GUIDE §3; api.yaml |
| Projected earnings dates | Earnings calendar | no formal gate | "Projection" with ± window (vs "Announced", "Provider est.") | USER_GUIDE §3 |
| Post-earnings drift signal (PEAD) | Earnings tab, Backtest Lab | no formal gate | model-based | api.yaml |
| News sentiment (FinBERT English, XLM-R other languages) | News & Sentiment, News Terminal | no formal gate | sentiment is model-based | api.yaml; ARCHITECTURE §11 |
| "Lazy Prices" text-change flag | News & Sentiment | no formal gate | flag on large year-over-year risk-factor text changes | USER_GUIDE §3 |
| Smart-money combined score | Smart Money, Ownership tab | sanity check only: no significant rank IC (21d −0.004, t −0.67; 63d −0.005, t −0.56; 2023-06 → 2026-10) | MODEL-BASED; descriptive screen, not an alpha signal; optional scoring input off by default | SMART_MONEY §4 |
| Hedge-fund-like filers, smart cohort, crowding, squeeze risk, FTD spike | Smart Money | no formal gate | model-based (our definitions) | SMART_MONEY §2 |
| CFTC COT index, z-scores, extremes, crowding, divergence, event study | Positioning, Cross-asset | no formal gate; source data official | official data; extremes / crowding / divergence / event study model-based (labelled) | CFTC_PLAN; CROSS_ASSET_DEFINITIONS §2b; api.yaml |
| Cross-asset trend signal, curve shape | Cross-asset dashboard | no formal gate | MODEL-BASED | CROSS_ASSET_DEFINITIONS §2 |
| Risk-on / risk-off composite | Cross-asset, Markets overview | no formal gate; sanity checks 2008-10-15 −2.8, 2020-03-20 −2.5 (risk_off), 2017-06-15 +0.3 | MODEL-BASED | CROSS_ASSET_DEFINITIONS §3 |
| Supply-chain links (disclosed) | Network & Geo | no formal gate (rule-based extraction with provenance) | disclosed customer / supplier with source | SUPPLY_CHAIN §1 |
| Statistical economic neighbours | Network & Geo | no formal gate | labelled statistical; co-movement, not a relationship | SUPPLY_CHAIN §3 |
| Country risk and shock propagation | Network & Geo, Supply chain | no formal gate | model-based | SUPPLY_CHAIN §4–5; api.yaml |

### Macro and events

| Model / output | Where shown | Gate verdict and key numbers | Label in the app | Source |
|---|---|---|---|---|
| Release expectations (SARIMA / seasonal naive) | Calendar, release pages, cockpit | no formal gate; SARIMA beat naive on 120 of 168 series | "Model-based expectation (not consensus)" | MACRO_V2_PLAN W2.2 |
| Surprise indices | Cockpit, themes, countries | no formal gate | vs model-based expectations (not consensus) | MACRO_V2_PLAN W2.3 |
| GDP nowcasts (DynamicFactorMQ; US, EA, GB, JP) | Nowcast page | no formal gate; track record shown next to GDPNow and NY Fed; 2026Q2 error +1.04 pp vs GDPNow +0.04 pp | model-based, with official benchmarks | MACRO_V2_PLAN W2.4; AUDIT F27 |
| Recession panel | Cockpit | no formal gate | each method labelled official or model-based (ours) | MACRO_V2_PLAN W2.5; `/macro/v2/recession` |
| HMM macro regime (4 states) | Cockpit, Markets, macro factor | no formal gate; the audit found over-confident posteriors (99.99% "Expansion") | model-based | ARCHITECTURE §11; AUDIT F07, F08, F38 |
| Cycle clock | Cockpit, countries | no formal gate (documented rule, cycle_v1) | method shown | MACRO_V2_PLAN W1.2 |
| Central-bank implied path | Central banks, WIRP | no formal gate | "Approximation from official curves (not futures/OIS-implied probabilities)" | `/macro/v3/central-banks` labels |
| TP-adjusted path, Taylor rule, expected inflation, tone | Central banks | no formal gate | model-based (see the labels in [Model-based versus official data](#model-based-versus-official-data)) | `/macro/v3/central-banks` labels |
| r* estimates | Central banks | official (NY Fed Laubach–Williams, HLW) | official | MACRO_V3_PLAN W3.4 |
| Rate cycle | Central banks | no formal gate | rule-based | `/macro/v3/central-banks` labels |
| Macro exposures (betas) | Macro tab, Exposures, Screener columns | no formal gate | "Model-based: rolling weekly OLS betas (156 weeks, >= 52), Newey-West HAC(4)" | `/macro/v4/exposures` |
| Regime-conditional performance | Regime performance | no formal gate (historical averages with hit rates, t-stats, n) | descriptive | MACRO_V3_PLAN W4.3 |
| Scenario builder and episode replay | Scenarios, Episodes | no formal gate | model-based, 90% bands | MACRO_V3_PLAN W4.4 |
| Our geopolitical-risk index (GPR) | Global Events | compared with the published Caldara–Iacoviello index, no pass / fail: daily global correlation 0.32 (7-day average 0.75); monthly country cross-section Spearman median 0.88 (2024-09 → 2026-10) | model-based | EVENTS_MODELS_REALTIME C3; `gpr_validation_20261020.json` |
| Escalation probabilities (Hawkes) | Global Events | no formal gate | model-based | EVENTS_MODELS_REALTIME C1 |
| Change points (BOCPD) | Global Events | calibrated on synthetic shifts: about 0.13 false alarms per 200 days, 97% detection of 1.5 sd shifts | model-based | EVENTS_MODELS_REALTIME C2 |
| Hotspots, spillovers, connectedness, impulse responses | Global Events | no formal gate (permutation inference for Moran / LISA) | model-based | EVENTS_MODELS_REALTIME B1–B4 |
| Event-study library and expected reaction | Global Events, News Terminal impact | no formal gate | model-based | EVENTS_MODELS_REALTIME C4 |
| Company event exposure | Company 360 › Event risk | no formal gate | model-based | EVENTS_MODELS_REALTIME C5 |
| GDELT event labels | Global Events, News | spot check: security links 82% (49 / 60), country 84%, theme 68%, sector about 68% | labels shown with sources | EVENTS_V2_PLAN E5 |
| Thesis automatic status | Thesis Tracker | no formal gate | "Automatic status (model-based rules)" | api.yaml |
| Model Studio methods, your own studies | Model Studio | no gate; guardrails (point in time, sample-size checks, multiple-testing warnings) | your result; method diagnostics | MODEL_STUDIO_PLAN |
| Your Backtest Lab strategies | Backtest Lab | no gate; DSR, PSR and PBO reported for you to judge | experimental signals marked | [Backtest Lab](11-backtest-lab.md) |
