# Technicals v2: T6 validation gate (T2 setup probabilities, T3 zone hold, T4 calibrated score)

Plan: `docs/TECHNICALS_V2_PLAN.md`. Code: `backend/app/techprob/` (`barriers.py` T2, `zone_model.py` T3, `score_cal.py` T4,
`history.py` point-in-time reconstruction, `calib.py`, `leakage.py`, `job.py`, `serve.py`). Scripts:
`scripts/technicals_v2/` (`wf_build.py`, `wf_t2.py`, `wf_t3.py`, `wf_t4.py`, `wf_leakage.py`, `econ.py`, `report.py`,
`wf_chain.ps1`). Raw results: `data/reports/technicals_v2/` (`t2_summary.json`, `t2_oos.parquet`, `t3_summary.json`,
`t3_oos.parquet`, `t4_summary.json`, `t4_oos.parquet`, `leakage.json`, `econ.json`, `econ_returns.parquet`, `gate.json`
= the verdicts consumed by the weekly fit, `summary.md` = every generated table, `reliability_*.svg` = reliability
diagrams). The point-in-time build itself is in `scratchpad/tech_v2_models/wf/` (seed of the weekly fit).

Everything is **model-based** and **marginal over history**: "calibrated on history", never "X% chance today".

## Verdict

| Output | ECE (gate <= 2.5 pp) | Worst regime (gate <= 5 pp) | Better than baselines (Brier and log-loss) | Leakage | Gate | Shown as |
|---|---|---|---|---|---|---|
| **T2 P(win / loss), H = 5** | 0.67 / 0.89 | **Covid 2020: loss +5.6** | yes (Brier 0.4575 vs Brownian 0.4650 / R:R-only 0.6494) | pass | **FAIL** | experimental |
| **T2, H = 10** | 0.91 / 1.03 | **Covid 2020: loss +5.1** | yes (0.4253 vs 0.4297 / 0.4801) | pass | **FAIL** | experimental |
| **T2, H = 20** | 1.13 / 1.47 | Covid 2020: win -4.6 / loss +4.0 | yes (0.3741 vs 0.3759 / 0.3834; log-loss 0.617 vs 0.648 / 1.073) | pass | **PASS** | production |
| **T2, H = 40** | 1.96 / 2.14 | Covid 2020 3.9 | **no** (Brier 0.3485 vs Brownian 0.3482) | pass | **FAIL** | experimental |
| **T2, H = 60** | **2.68 / 2.73** | Covid 2020 2.8 | **no** (0.3453 vs 0.3429 / 0.3430) | pass | **FAIL** | experimental |
| **T3 P(hold \| touch)** | 1.45 | **Covid 2020: -8.7** (2020 full year -4.6) | yes (Brier 0.2410 vs climatology 0.2470 / old strength 0.2470) | pass | **FAIL** | experimental |
| **T3 edge vs random (per-zone flag)** | bucket predicted vs realised edge within 1.9 pp | n/a | level features beat the random-control model: Brier -0.00045 [-0.00066, -0.00025] | pass | **PASS** | production |
| **T4 score -> outcomes, 5 / 20 / 60 d** | bucket hit-rate mapping error 1.1 / 1.5 / 1.8 pp | n/a | **no** (rank IC -0.003 / -0.012 / -0.014, t -0.7 / -0.9 / -1.0) | pass | **FAIL** | experimental; card says "no out-of-sample edge" |

**Economic check (Backtest Lab engine, costs on): null result.** Filtering a simple setup-following strategy by
expected R > 0 did **not** significantly improve it over taking every setup (+1.0%/yr at H = 20, t = 1.33; -0.6%/yr at
H = 10, t = -0.75). It is significantly better than the naive "R:R >= 1.5" rule (+5.6 / +5.8%/yr, t = 3.4 / 3.2), which
itself is the worst variant. PBO over the 10 configurations = 0.07; DSR ~0.99 for every configuration (the trial
Sharpe variance is small, so DSR does not discriminate, as in the conformal gate).

**Reading.**
- **T2 is well calibrated where it matters and beats both baselines up to 20 sessions.** The empirical two-barrier
  surface is within 1.5 pp everywhere at H <= 20 and in every region, vol regime, earnings window, VIX > 25, 2020 and
  2022. The naive R:R rule is badly miscalibrated at short horizons (ECE 15.8 pp at H = 5: it ignores "neither by H");
  the driftless Brownian formula is 1.2-2.7 pp too pessimistic on P(win) at H <= 10 (equities drift up, and the
  standardisation by today's sigma mean-reverts). Only H = 20 passes the full gate: at 5 and 10 sessions the
  Feb-Jun 2020 crash window had 5.1-5.6 pp more stop-outs than predicted (as in the conformal gate, a sudden crash
  arrives after the origins), and at 40 / 60 sessions the grid loses its edge over the Brownian formula (the
  probabilities converge to the gambler's-ruin limit, where the baselines are already right).
- **Expected R is well ordered and roughly calibrated** (H = 20 deciles: predicted -0.33 ... +0.085 R, realised
  -0.32 ... +0.030 R; E[R] > 0 setups realised +0.011 R, E[R] <= 0 setups -0.093 R). The top decile is optimistic by
  ~0.05 R. But the economic test shows the filter's value is not significant after costs in a portfolio.
- **T3:** the hold classifier is calibrated (1.45 pp) and better than climatology and the old strength (which carries
  no information beyond the base rate: Brier identical to climatology). It fails the gate on Covid 2020 (-8.7 pp:
  zones broke far more often than predicted). The **edge vs random is the honest headline**: on average, touched
  confluence zones hold **less** often than random price bands with the same width, approach, trend and regime
  (-0.63 pp vs the control model, CI [-1.26, -0.01]; raw rate 55.5% vs 56.9%). The level features do carry information
  (they beat the control model on Brier, CI excludes 0) and sort zones into buckets with significant realised edges:
  top quintile +2.1 pp [+1.4, +2.7] ("holds more often than random"), bottom two quintiles -4.4 pp and -1.3 pp
  ("holds less often than random"), middle two not significant ("no reliable edge"). That per-zone flag passed.
- **T4:** the composite technical score has **no out-of-sample edge**; if anything it is weakly mean-reverting (deciles
  monotone *downward*: Spearman -0.84 to -0.94, but the top-bottom spread CI includes 0). The sub-scores saturate
  (structure is 0 or 100 on 100% of origins, trend has 5 distinct values with 38% at 100, momentum 68% at 100);
  the percentile versions fix the presentation but not the predictive content (IC of the percentile composite also n.s.).

## Setup

- **Universe:** the conformal gate universe: mvp US (490 names with >= 300 bars) plus the mvp_global EU (90), UK (27)
  and JP (40) names; adjusted daily bars 2003 -> 2026-10-21 on each security's own sessions; test years 2010 -> 2026.
  Current mvp membership and sector classification throughout (survivorship bias, as documented for the conformal gate).
- **Point-in-time reconstruction (`app.techprob.history`).** For every security, ONE `build_levels` over the full
  history, viewed at each weekly origin (last session of the ISO week, 2008 ->) with `levels.snapshot(at = i + 1)`:
  level instances with start <= i + 1 < end, series values computed from bars <= i, touch counts on bars <= i.
  Verified identical to a rebuild on truncated bars except volume-profile levels, whose 5-bar refresh grid is anchored
  at the series end (0-4 bar phase difference; still point-in-time). Weights `level_weight_expr` at the origin date;
  level-type reliability **neutral** (level_type_stats has no point-in-time history; documented deviation - live zones
  use it). Then the live code: `context(bars <= i)`, `find_zones`, `build_setup`. 562k weekly setups, 3.43M touched
  zone rows (1.74M real, 1.69M random control).
- **Volatility units.** The conformal envelope's EWMA (lambda 0.94, earnings reaction days excluded) and jump variance;
  V(H) = H s2 + J 1[earnings day within H]; vol regime = sigma_d / median sigma_d of the previous 252 sessions
  (low < 0.85 <= mid <= 1.2 < high). Realised earnings_events dates used as if pre-announced (as in the conformal gate).
- **Regimes:** Covid 2020 = origins 15 Feb - 30 Jun 2020; year 2020; 2022; VIX > 25 (FRED VIXCLS at the origin);
  earnings window; vol regime; region; long / short (T2); support / resistance / in-zone (T3). Gate on regimes with
  n >= 2000: |realised - predicted| (calibration in the large).
- **Metrics:** ECE over 15 quantile bins (probability units; table values in pp), Brier and log-loss (3-class
  win / loss / neither for T2), month-block bootstrap CIs (300-400 resamples).

## T2: two-barrier first-passage probabilities

**Event.** From the origin close, T1 (upper for a long) and the invalidation (lower) as barriers; on the next H own
sessions the high reaching the upper barrier = up, the low reaching the lower = down; both first on the same bar =
tie (counted as a loss: intrabar order unknown); otherwise neither. Distances standardised: a = log(upper / close) /
sigma_H, b = log(close / lower) / sigma_H. A short mirrors it.

**Estimators (chosen by out-of-sample reliability):**
- **grid (production):** for every daily origin of the universe (3.2M daily origins x 5 horizons) the outcome of every (a, b)
  pair on a 20 x 20 log grid (0.1 ... 8 sigma_H), counted per region x vol regime x earnings window and per origin
  quarter. Fit for test year Y on quarters < Q(Y) - 1 (one-quarter purge). Shrinkage pool -> region -> all regions
  (m = 500 pseudo-origins), monotone smoothing (iterated PAV), bilinear in (log a, log b). The mean terminal move of
  "neither" paths is kept per cell (marked-to-market expected R).
- **gbm (candidate):** XGBoost multiclass on the setups of years <= Y-2 (log a, log b, vol regime, earnings, region,
  VIX > 25, direction, trend / structure sub-scores), isotonic calibration on year Y-1. Better calibrated at 40 / 60
  sessions (ECE 0.5 pp) but worse Brier at 5-20 and still worse than the Brownian baseline at 40 / 60: not deployed.
- **Synthetic check (pytest):** on Brownian paths the grid estimator matches the analytic two-barrier probability
  within 1.5 pp (discrete-monitoring correction applied).

| method | H | n | ECE win | gap win | ECE loss | gap loss | Brier (3-class) | log-loss (3-class) |
|---|---|---|---|---|---|---|---|---|
| grid | 5 | 507,860 | 0.67 | +0.56 | 0.89 | +0.55 | 0.4575 | 0.7832 |
| grid | 10 | 507,213 | 0.91 | +0.79 | 1.03 | +0.85 | 0.4253 | 0.7304 |
| grid | 20 | 505,879 | 1.13 | +0.93 | 1.47 | +0.39 | 0.3741 | 0.6173 |
| grid | 40 | 502,883 | 1.96 | +1.26 | 2.14 | -0.70 | 0.3485 | 0.5441 |
| grid | 60 | 500,301 | 2.68 | +1.85 | 2.73 | -1.57 | 0.3453 | 0.5290 |
| gbm | 20 | 505,879 | 0.75 | +0.66 | 0.72 | +0.63 | 0.3771 | 0.6218 |
| gbm | 60 | 500,301 | 0.52 | +0.46 | 0.47 | -0.18 | 0.3444 | 0.5254 |
| Brownian | 5 / 20 / 60 | | 2.87 / 1.23 / 1.04 | -2.66 / -1.22 / -0.80 | 2.01 / 1.21 / 1.03 | | 0.4650 / 0.3759 / 0.3429 | 0.8337 / 0.6476 / 0.5397 |
| R:R only | 5 / 20 / 60 | | 15.79 / 2.89 / 1.09 | -15.79 / -2.89 / -0.88 | 13.76 / 2.47 / 1.04 | | 0.6494 / 0.3834 / 0.3430 | 4.35 / 1.07 / 0.56 |

Brier difference grid - Brownian (month blocks): H = 5 -0.0075 [-0.0089, -0.0060], H = 10 -0.0044, H = 20 -0.0018
[-0.0033, -0.0001], H = 40 +0.0004 (n.s.), H = 60 +0.0024 (Brownian better).

Regime gaps (grid, realised - predicted, win / loss, pp): Covid 2020 H5 -1.9 / +5.6, H10 -4.2 / +5.1, H20 -4.6 / +4.0;
2020 <= 1.7; 2022 <= 3.7 (loss, H10); VIX > 25 <= 3.4; earnings window <= 3.0; every region, vol regime and side
<= 2.4. Reliability diagrams: `reliability_t2_h{5,10,20,40,60}.svg`.

**Expected R** (grid, Backtest Lab costs: commission 1 bp, ADV-bucket half spread, square-root impact on a $200k
position, borrow for shorts; R:R from the close): see the decile tables in `summary.md`; share of setups with
E[R] > 0: 46% (H5) ... 32% (H20); realised R of E[R] > 0 setups +0.031 / +0.017 / +0.011 / +0.023 / +0.027 R at
H = 5 / 10 / 20 / 40 / 60, of E[R] <= 0 setups -0.09 to -0.11 R.

## T3: zone hold / break

**Definitions (sigma = EWMA sigma_d at the origin, multiplicative in price).** Touch = the near edge reached within 20
sessions (support: low <= zone_high; a zone containing the close is touched at the origin). Break = a close beyond
the far edge by >= 0.5 sigma (the touch bar's close counts; checked before the hold test on each bar). Hold = a later
bar's high >= zone_high e^{1.5 sigma} (support; mirrored for resistance) before any break, within 10 sessions of the
touch. Label hold = 1, else 0 (break or unresolved). Random control: one band per real zone, same width, centre
shifted by +-U(0.75, 2.5) ATR (the level_type_backtest control), labelled by the same rule.

**Features:** level-family presence (14 families), member / type / family counts, relative KDE density, width in
sigma, distance to the near edge in sigma, approach speed (5 and 20-session return toward the zone in sigma), trend
and structure state signed by side, RSI, distances of the zone centre to SMA50 / SMA200 (ATR) and MA-inside flags,
close vs SMA200, distance to the nearest volume-profile level, prior touches / resolved tests / hold rate of the same
band over 252 sessions, vol regime, VIX > 25, earnings within 20 sessions, region, side, in-zone. The old strength and
weight sum are NOT inputs (their scale differs between the neutral-reliability history and the live zones); the old
strength is kept as a baseline only.

**Protocol:** purged walk-forward, 2-year test blocks 2010-2026; training origins < block start - 425 days,
calibration year [start - 365 d, start - 60 d) for isotonic calibration; <= 800k training rows per model. Real-zone
models (logistic, HistGradientBoosting) and the same pipeline on the control bands (band features only).

| model (real zones, OOS) | ECE | gap | Brier | log-loss |
|---|---|---|---|---|
| **hgb (chosen)** | 1.45 | -0.01 | 0.2410 | 0.6754 |
| logit | 1.38 | -0.10 | 0.2414 | 0.6760 |
| control model on the real zones' band features | 1.22 | -0.63 | 0.2415 | 0.6777 |
| climatology | 0.87 | -0.02 | 0.2470 | 0.6872 |
| old strength (isotonic) | 0.74 | +0.17 | 0.2470 | 0.6871 |

Regimes (hgb): Covid 2020 -8.68 pp (ECE 10.2), 2020 -4.60, 2022 +0.83, VIX > 25 +0.33, support +1.44, resistance
-1.35, in-zone -0.40, vol regimes and regions within 1.2 pp. Reliability diagram: `reliability_t3_hold.svg`.

**Edge vs random (real zones, OOS, month blocks):**

| predicted-edge quintile | n | predicted edge | realised edge (hold - P_control) | 95% CI | significant |
|---|---|---|---|---|---|
| 1 | 333,880 | -5.6 pp | -4.4 pp | [-5.2, -3.6] | yes (worse than random) |
| 2 | 294,138 | -1.8 | -1.3 | [-1.9, -0.7] | yes (worse) |
| 3 | 285,055 | -0.4 | -0.2 | [-1.0, +0.5] | no |
| 4 | 347,176 | +0.6 | +0.7 | [-0.2, +1.6] | no |
| 5 | 328,293 | +4.0 | +2.1 | [+1.4, +2.7] | yes (better than random) |
| all | 1,588,542 | | **-0.63** | [-1.26, -0.01] | |

The live API flags each zone by its bucket: `edge_significant` + `edge_direction` (better / worse than random), else
"no reliable edge". Buckets are the pooled walk-forward quintiles (`gate.json` t3.buckets).

## T4: calibrated technical score

Percentile sub-scores: rank within region x sector on the same date (mid-rank for ties, 0..100); additive fields, the
original sub-scores are kept. Score -> outcome: technical_score deciles (boundaries from origins < Y - 90 days) ->
next 5 / 20 / 60-session excess log return vs the region's equal-weight universe mean.

| horizon | rank IC (OOS, all dates) | t (non-overlapping) | Spearman(decile, mean) | top - bottom decile | edge |
|---|---|---|---|---|---|
| 5 d | -0.0031 | -0.67 | -0.94 | -0.09% [-0.22, +0.03] | none |
| 20 d | -0.0117 | -0.91 | -0.84 | -0.35% [-0.72, +0.09] | none |
| 60 d | -0.0139 | -1.00 | -0.89 | -0.59% [-1.32, +0.12] | none |

Same picture for the percentile composite (IC -0.003 / -0.009 / -0.012, n.s.). Bucket hit rates span 49.5-52.1%;
the mapping from training buckets to the test year is accurate (hit-rate error 1.1-1.8 pp), so the statistics are
honest descriptions of history - but they are no better than the unconditional distribution, hence FAIL (shown only
with the experimental toggle; the card always says "no out-of-sample edge").

## Leakage

- **Causal invariance (structural):** rebuilding the point-in-time history after replacing every bar after a cut
  (shuffled, rescaled returns) leaves every feature of every origin before the cut bit-for-bit unchanged (4 real
  securities US / US / EU / JP x 2 cuts; + a synthetic series in pytest). A first run showed float-noise zone-boundary
  differences on one JP name: `confluence.merge_identical` grouped levels in hash order (non-deterministic KDE sum
  order). Fixed (`maintain_order=True`), re-run clean. Not a look-ahead; the walk-forward build predates the fix
  (effect: rare float-tie zone boundaries).
- **Peeking feature caught:** a zone feature that reads the close 5 sessions ahead is flagged on both test securities.
- **Label permutation:** T3 logit / hgb refitted on permuted labels: out-of-sample Brier skill -0.0007 / -0.0027
  (true labels +0.025); T2 GBM candidate: -0.0020; T4 rank IC vs permuted returns -0.0001.

## Economic check (`econ.json`)

US mvp, weekly long setups 2010 -> 2026-10, OOS grid predictions; entry next close, exit at the close where the trade
return reaches T1 / the invalidation (close-based, the engine has no intraday barriers) or after H sessions; max 100
positions x 1%; costs on.

| config | Sharpe | CAGR | max DD | trades | hit rate | stop / target / time exits |
|---|---|---|---|---|---|---|
| H20 all setups | 0.89 | 12.2% | -14.8% | 45,090 | 69.9% | 11,451 / 29,565 / 4,069 |
| H20 R:R >= 1.5 | 0.74 | 7.6% | -13.6% | 21,593 | 46.9% | 10,163 / 5,972 / 5,456 |
| **H20 E[R] > 0** | 0.96 | 13.3% | -17.4% | 39,769 | 64.0% | 12,046 / 23,306 / 4,410 |
| H20 E[R] > 0, ranked by E[R] | 0.97 | 14.0% | -17.0% | 36,153 | 58.7% | |
| H20 E[R] > 0.1 | 1.01 | 5.9% | -7.8% | 8,234 | 54.7% | |
| H10 all / R:R >= 1.5 / E[R] > 0 | 0.94 / 0.72 / 0.89 | 12.6 / 6.3 / 12.0% | | | | |

Paired (annualised, daily returns): E[R] > 0 - all: H20 +1.0% (t 1.33), H10 -0.6% (t -0.75); ranked - all: H20 +1.7%
(t 1.77); E[R] > 0 - R:R >= 1.5: H20 +5.6% (t 3.37), H10 +5.8% (t 3.17); E[R] > 0.1 - all: -6.8% / -10.8% (fewer
trades, mostly cash). Absolute Sharpes are largely long-only market beta. **Result: null for the E[R] filter.**

## Production configuration (what the jobs serve)

- **T2:** grid surface, refitted weekly on every complete daily origin of the gate universe (`tech_prob_surfaces`);
  served for every security with a setup (`tech_prob_setups`, all five horizons); H = 20 is production, 5 / 10 / 40 /
  60 are flagged experimental (hidden by default). The setup's own horizon (first H with T1 within one sigma_H) is
  marked. Barriers are measured from the close (market entry): the expected R uses R:R from the close; the card's
  R:R (from the limit entry) is kept for reference. Thesis target / stop: same surface, computed at query time.
- **T3:** HistGradientBoosting + isotonic, real and control models refitted weekly (`data/models/technicals_v2/`);
  P(hold | touch) is experimental (Covid regime), the per-zone edge flag is production.
- **T4:** percentile sub-scores always shown; score-bucket stats experimental, "no out-of-sample edge" stated.
- Verdicts are re-read from `gate.json` at every weekly fit (`tech_prob_gates`); re-running the chain
  (`scripts/technicals_v2/wf_chain.ps1`) and `report.py` updates them.

## Known limitations

- Current mvp membership (survivorship), current sectors, EU / JP earnings dates only from ~2014-2015.
- Neutral level-type reliability in the history (live zones are reliability-weighted): zone sets differ slightly.
- Volume-profile refresh phase (0-4 bars) differs from a live run.
- The engine's exits are close-based; the probability model uses intraday highs / lows (the barrier is "touched",
  not closed through), so realised backtest P&L and the model's win / loss events are not identical.
- Two estimators can disagree by a few pp: the thesis tracker's conformal touch frequency of the target (region x sector pool, envelope variance) and the T2 'target first' probability (region x vol-regime pool, own-session EWMA) are calibrated separately (seen: 18% touch vs 20% target-first for one 20-session example).
- The vol regime and the percentile cross-sections are computed on the gate universe in the walk-forward, on the full
  universe live.
