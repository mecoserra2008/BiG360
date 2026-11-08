# Technicals v3: validation gate (Bundle B)

Ideas: `docs/TECHNICALS_V3_IDEAS.md` (Bundle B = #1 evidence-based factors, #2 meta-labelling, #5 volume-confirmed
breakouts). This document holds the gate of **#1 and #5**; the **Meta-labelling (#2)** section belongs to the #2 work
and is appended below it.

Code #1 / #5: `backend/app/techv3/` (`factors.py`, `breakouts.py`, `build.py`, `panel.py`, `stats.py`, `bo_model.py`,
`job.py`, `serve.py`). Scripts: `scripts/technicals_v3/` (`wf_build.py`, `wf_eval.py`, `wf_breakouts.py`, `econ.py`,
`spreads.py`, `report.py`, `wf_chain.ps1`). Raw results: `data/reports/technicals_v3/` (`eval.json` every IC cell,
`ic_series.parquet` per-date IC series, `buckets.parquet` decile means, `bo_summary.json`, `bo_oos.parquet`,
`reliability_bo_h{10,20}.svg`, `econ.json`, `econ_returns.parquet`, `spreads.json`, `gate.json` = the verdicts the jobs
read, `eval_rows.parquet` = the validation rows of `tech_factor_eval`). Research build: `scratchpad/tech_v3_b15/wf/`.

## Verdict

### #1 Evidence-based technical factors (rank IC of the percentile vs the sector-neutral 20-session forward log return; pooled US / EU / UK / JP, weekly origins 2005-01 -> 2026-10)

| Factor | Sign | IC 5d (t) | **IC 20d (t)** | IC 60d (t) | Holm p (20d) | Monotone deciles / spread CI | Regions aligned (sig. wrong) | Years aligned | Shift-by-one keeps | Permutation | Worst regime | Economics (US L/S net Sharpe; LO vs EW) | **Gate** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 52-week-high proximity (hi52) | + | +0.017 (3.7) | **+0.023 (2.9)** | +0.039 (3.3) | 0.017 | 0.82 / +1.3% [+0.9, ...] | 4 of 4 (none) | 82% | 104% | null | GFC 2008-09 -0.069 (t -1.6) | -0.27; -1.5%/yr (t -0.8) | **PASS** |
| 12-1 momentum (mom) | + | +0.013 (3.4) | +0.011 (1.6) | +0.010 (0.9) | 0.30 | 0.36 | 3 of 4 (JP -0.013, t -1.7) | 73% | 98% | null | GFC -0.059 | -0.17; -0.1%/yr | FAIL (G1, G2) |
| Residual momentum (rmom) | + | +0.008 (3.1) | +0.004 (1.1) | -0.000 (-0.1) | 0.56 | 0.84 | 3 of 4 | 77% | 95% | null | | -0.28; +0.1%/yr | FAIL (G1) |
| 1-month reversal (strev) | - | -0.013 (-4.3) | -0.015 (-3.6) | -0.010 (-2.0) | 0.002 | **0.35** | 4 of 4 | 82% | 89% | null | | -0.66; -4.7%/yr (t -2.5) | FAIL (G2: not monotone) |
| MAX (maxret) | - | -0.030 (-8.3) | **-0.038 (-6.4)** | -0.054 (-6.1) | 9e-10 | 1.00 / +2.1% [+1.7, ...] | 4 of 4 | 82% | 99% | null | Covid 2020 +0.038 (n.s.) | -0.35; -2.0%/yr (t -1.1) | **PASS** |
| Idiosyncratic volatility (ivol) | - | -0.031 (-9.1) | **-0.044 (-7.5)** | -0.063 (-6.8) | 6e-13 | 1.00 / +2.4% [+2.0, ...] | 4 of 4 | 86% | 99% | null | Covid 2020 +0.055 (n.s.) | -0.28; -0.7%/yr (t -0.4) | **PASS** |
| Industry momentum (indmom) | + | +0.007 (2.2) | +0.003 (0.6) | +0.007 (1.1) | 0.56 | 0.71 / CI incl. 0 | 3 of 4 | 55% | 99% | null | | n/a (sector-neutral portfolio) | FAIL (G1-G3) |
| **Composite (comp_wf, pre-declared)** | + | +0.028 (7.3) | **+0.035 (5.3)** | +0.045 (4.1) | single test p < 1e-6 | 1.00 / +1.8% [+1.3, ...] | 4 of 4 (EU / UK n.s.) | 89% | n/a | null | GFC -0.039 (n.s.) | **-0.46; -1.9%/yr (t -1.1)** | **FAIL** (G6 vs v2: t 1.66; G7 economics) |

Spread = sign-aligned top-minus-bottom decile mean of the sector-neutral 20-session LOG return (95% month-block CI
lower bound shown). IC values are raw (negative for the negative-sign factors). "Shift-by-one keeps" = IC of the
factor dated one session earlier / IC of the factor (same labels). "Permutation null" = 20 within-date label
permutations: mean IC within +-0.0004 of 0 and at most 10% of draws with |t| >= 2, for every factor.

**Multiple testing.** 105 factor cells were tested (7 factors x 3 horizons x (4 regions + pooled)) plus 45 composite
cells (3 variants x 3 x 5). Benjamini-Hochberg over the 105 factor cells: 49 significant at q = 0.05, every one with
the literature's sign. The per-factor verdict uses Holm over the 7 primary (pooled, 20-session) cells: hi52 0.017,
strev 0.002, maxret 9e-10, ivol 6e-13 pass G1; mom 0.30, rmom 0.56, indmom 0.56 do not.

**Shown by default: hi52, MAX, IVOL** (labelled "rank edge", see the economics). Everything else is experimental
(hidden unless the viewer enables experimental outputs). The composite failed, so the Technical score card keeps the
honest "no out-of-sample edge" heuristic score; no composite value is served.

### #5 Volume-confirmed breakouts (liquid US / EU / UK / JP, 20 / 55-session breakouts 2005 -> 2026, 1.67M events, follow-through = +1 sigma_H beyond the breakout close before a close back inside the level)

| Output | H | ECE (<= 2.5 pp) | Worst regime (<= 5 pp) | Beats climatology & geometry (Brier / log-loss) | Permuted labels | Gate |
|---|---|---|---|---|---|---|
| Calibrated P(follow-through) | 10 | 1.23 | **Covid 2020 +18.3 pp** (2020 +6.8) | yes: 0.1810 vs 0.1867 / 0.1823; Brier full - geometry -0.0012 [-0.0015, -0.0009] | no skill (0.1868 = climatology) | **FAIL** -> experimental |
| Calibrated P(follow-through) | 20 | 1.20 | **Covid 2020 +17.2 pp** (2020 +6.5) | yes: 0.1603 vs 0.1651 / 0.1610; Brier full - geometry -0.0006 [-0.0009, -0.0004] | no skill | **FAIL** -> experimental |

| Effect (H = 20) | Estimate | 95% CI (month blocks) | Gate |
|---|---|---|---|
| Base rate (all breakouts) | 21.1% | [20.1, 22.0] | |
| Confirmed (relvol50 >= 1.5 and close in the top quarter of the range) / unconfirmed | 26.2% / 19.6% | | |
| Raw difference confirmed - unconfirmed | **+6.6 pp** | [+6.1, +7.2] | |
| Geometry-adjusted difference (strata: distance beyond the level x vol regime x N x direction x region) | **+1.6 pp** | [+1.2, +2.0] | |
| Confirmed - geometry-matched random control | **+3.5 pp** | [+2.7, +4.6] | |
| Unconfirmed - its random control | +0.6 pp | [-0.1, +1.4] | |
| **Volume confirmation edge** | | | **PASS** (both CIs > 0; H = 10 too: +1.8 pp [+1.4, +2.3], +5.8 pp [+5.0, +7.0]) |
| Economic check (US up breakouts, engine, costs on) | confirmed Sharpe -0.35 vs all -0.61; confirmed - random subset of the same size **-1.0%/yr (t -0.6)**; PBO 0.12 | | no tradable edge |

**Reading #5.** Volume-confirmed breakouts DO follow through more often than unconfirmed ones and than the base rate,
and the effect survives the two controls that matter: most of the raw +6.6 pp is geometry (confirmed breakouts close
much further beyond the level: median b = 0.23 vs 0.10 sigma_H), but +1.6 pp remains within geometry strata, and the
confirmed events beat geometry-matched random levels by +3.5 pp while unconfirmed breakouts are no better than random
levels. The confirmation score is monotone (quintiles 17.7 / 18.7 / 19.7 / 22.0 / 27.2%). The effect is small in money:
breakout trading is negative after costs (fails back inside ~70% of the time), and the confirmed subset is no better
than a random subset of the same size (its gain vs "all" comes from fewer trades). The calibrated P(follow-through) is
well calibrated overall and adds information beyond geometry, but misses the Covid 2020 window by 17-18 pp (breakouts in
the rebound followed through far more often than predicted), so it is shown only as experimental. The state chip and the
confirmation flag / score (deterministic) are shown; the probability is hidden unless the experimental toggle is on.

## Setup

- **Universe.** Every equity (securities_master common / ADR) with prices in US, EU, UK, JP: 6,017 / 1,357 / 333 / 3,674
  securities, 2001 -> 2026-10 (factors from 2004, evaluation from 2005). Securities that stopped trading are included while
  they traded (prices_daily keeps them), but listing coverage is better for current names (survivorship bias remains).
  Sectors: current issuers.sector_id ('NA' = unclassified -> region-level ranks).
- **Liquidity filter (the validated universe).** 20-session ADV >= $1M (USD) and close >= $1 on the origin date; on average
  3,700 names per weekly cross-section across the four regions. Market cap is not point-in-time outside the ~700-name mvp
  universe (valuation_daily), so **size buckets use the 60-session dollar volume**: large >= $50M, mid $10-50M, small < $10M;
  **micro** = ADV $0.2-1M, excluded from the main evaluation and reported separately.
- **Point in time.** Every factor dated t uses bars <= t (trailing windows; structural causal-invariance tests in
  `tests/techv3`); labels start at the close of t + 1 (the pit_rule execution lag): fwd_h = log C[t+1+h] - log C[t+1].
- **Labels.** Sector-neutral: fwd_h minus the equal-weight mean of the liquid names in the same region x sector x date
  (region x date for thin / unclassified sectors and for industry momentum, a between-sector signal).
- **IC.** Per (region, date) Spearman of the within-sector percentile vs the label; pooled per date weighting regions by
  names; Newey-West t with ceil(h / 5) + 1 lags. Weekly origins (last session of each ISO week): 1,216 dates.
- **No fitted parameter.** Definitions and signs are pre-registered from the literature, so the whole sample is
  out-of-sample for each factor. The composite is built **walk-forward**: for test year Y the equal-weight mean of the
  sign-aligned percentiles of the stock-level factors whose pooled 20-session IC up to 30 days before Jan 1 of Y is
  sign-aligned with t >= 2 (first test year 2008). Selected sets: 2008-09 {hi52, mom, rmom, maxret, ivol}, 2011-24 {strev,
  maxret, ivol} (+ hi52 from 2025). Live members today: hi52, strev, maxret, ivol. Variants reported: comp_all (all six,
  no selection) and comp_ridge (expanding ridge, lambda = 0.01 n, months up to November of Y - 1).
- **Regimes.** Covid 2020 (15 Feb - 30 Jun), 2022, VIX > 25 / <= 25 (FRED VIXCLS), bear / bull market (the region's
  equal-weight market 12-month return < 0 / >= 0), GFC 2008-09, eras 2005-09 / 2010-19 / 2020-26.

### Pre-registered gate (written before reading results; `scripts/technicals_v3/report.py`)
Factor PASS = all of: **G1** pooled 20-session IC sign-aligned and Holm p < 0.05; **G2** sign-aligned decile monotonicity
>= 0.7 and top-bottom spread CI > 0; **G3** aligned in >= 3 of 4 regions, no region significantly wrong-signed (BH),
aligned in >= 60% of years; **G4** shift-by-one keeps >= 50% of the IC, permutation mean |IC| < 0.005 and <= 15% of
draws |t| >= 2; **G5** no regime with aligned t <= -2. Composite PASS = G1 (single test) + G2-G5 + **G6** beats the v2
heuristic technical score on the same rows (paired IC difference NW t >= 2) + **G7** US long-short net Sharpe > 0 with
PSR >= 0.95 and the long-only quintile beats the equal-weight universe. P(follow-through) PASS = ECE <= 2.5 pp, every
regime with n >= 2000 within 5 pp, better Brier / log-loss than climatology and geometry (CI of the Brier difference vs
geometry < 0), permuted-label skill <= 0. Confirmation edge PASS = geometry-adjusted difference CI > 0 and confirmed -
matched control CI > 0. Economics of single factors is reported (`tradable_after_costs`), not part of the factor gate.

## #1 details

**Size buckets (20d IC, t):** hi52 large +0.011 (1.2), mid +0.017 (2.0), small +0.030 (4.0), micro +0.053 (6.8); MAX
-0.015 (-1.9) / -0.030 (-4.8) / -0.049 (-9.3) / micro -0.064 (-12.6); IVOL -0.019 (-2.7) / -0.037 (-6.2) / -0.054 (-9.8)
/ micro -0.073 (-12.8); reversal -0.010 / -0.010 / -0.018 / micro -0.011. Every effect is strongest in small and micro names
and weakest (hi52: n.s.) in large caps.

**Regions (20d IC, t):** hi52 US +0.025 (2.8), EU +0.014 (1.6), UK +0.009 (1.0), JP +0.027 (3.0); MAX US -0.036 (-5.0),
EU -0.010 (-1.6), UK -0.002 (-0.3), JP -0.062 (-9.4); IVOL US -0.041 (-5.6), EU -0.012 (-2.0), UK -0.006 (-0.9), JP
-0.070 (-10.4); momentum works in EU (+0.032, t 3.7) and UK (+0.030, 3.2), not in JP (-0.013) - the known Japanese
momentum anomaly - which is why it fails pooled; residual momentum likewise (EU +0.016, t 3.1).

**Regimes.** The low-risk factors (MAX, IVOL) carry the edge in calm markets (VIX <= 25: -0.045 / -0.051, t -7.5 / -8.5)
and disappear when VIX > 25 (+0.002 / -0.004, n.s.; Covid 2020 wrong-signed but not significant); hi52 and momentum
lose in the GFC (-0.069 / -0.059, t -1.6 / -1.8) and in VIX > 25 (-0.048 / -0.022, n.s.). No regime is significantly
wrong-signed for a passing factor (G5). Yearly: hi52 aligned in 18 of 22 years (worst 2009 -0.139), MAX / IVOL in 18 /
19 of 22 (wrong in 2009, 2010, 2016, 2020).

**Leakage.** Shift-by-one retains 89-104% of every factor's IC (the information is not concentrated in the last print);
20 label permutations give mean IC within +-0.0004 of zero; the positive control (a "factor" equal to the realised
5-session label) shows IC = 1.0 and 0.000 (t -1.0) after permutation, so the pipeline detects leakage when present.
Causal invariance (pytest): replacing every bar after a cut leaves every factor <= the cut bit-for-bit unchanged; a
one-session peek is caught.

**Composite vs the v2 heuristic score** (the mvp gate universe of the v2 walk-forward, 556k weekly rows, 646 names, the
same sector-neutral labels): 20d IC comp_wf -0.005 (t -0.6) vs v2 score -0.020 (t -3.3); difference +0.015 (t 1.66,
p 0.10) -> G6 fails. On large liquid names the composite has no edge (the factor effects are small-cap effects), and the
v2 score is weakly mean-reverting there. 5d: +0.013 (t 2.4); 60d: +0.011 (t 0.8).

**Economics (Backtest Lab engine, monthly, sector-neutral quintiles, equal weight, liquid names, USD, 2008-01 -> 2026-10;
costs: 1 bp commission, ADV-bucket half spread, sqrt impact on $10M, borrow 50 / 300 bp, FX 2 bp; daily |log return| > 1
print errors zeroed, as in the factors).** No factor and no
composite is profitable as a stand-alone strategy after costs: US long-short net Sharpes -0.17 (mom) to -0.66 (reversal),
gross -0.03 to +0.25; one-way turnover 7.6x (mom) to 18.8x (reversal) per year costs 4-10%/yr. EU / UK / JP are worse
(net -0.4 to -1.3). Long-only top quintiles track the equal-weight universe (US EW Sharpe 0.41; hi52 / IVOL / composite long-only
0.4-0.5) and do not beat it (+0.1 to -4.7%/yr, US t +0.1 to -2.5). DSR = 0 for every long-short config (negative
Sharpes); PBO 0.0 per region (the in-sample best config also ranks well out of sample - consistently bad). Why the
rank IC does not translate: the IC is a median / rank effect on LOG returns; simple-return decile spreads are much
smaller (hi52 +0.08%, MAX +0.23%, IVOL +0.25% per 20 sessions vs +0.98 / +1.31 / +1.50% in logs: `spreads.json`) because
the high-volatility deciles carry the variance drag and a fat right tail. Industry momentum's sector-neutral portfolio
holds nothing (constant within a sector): its economics is not meaningful in this construction.

So the passing factors are **descriptive rank evidence** ("historically, stocks in this percentile had lower / higher
next-month returns than their sector peers, in median"), labelled "rank edge" in the UI with the economic note - not
a strategy.

## #5 details

**Definitions.** Up breakout of lookback N (20 / 55): C[t] > max(High[t-N..t-1]) and the previous close was not, no up
event of the same N in the previous 10 sessions; level L = that prior high (mirrored for down). sigma = EWMA (0.94)
daily log-return vol at t - 1; target = C[t] exp(dir sigma sqrt(H)); success = high (low) reaches the target before a
close back inside the level; fail = close inside first (a bar with both = fail); neither = neither by H. Features at
the close (dir-signed): relvol20 / relvol50, close location, CMF20, OBV slope / OBV at its own N-extreme, Amihud
illiquidity change (20 vs prior 60 sessions), opening gap and gap beyond the level, day return / range, trend vs
SMA200, prior 20-session run-up, vol ratio. Random control: one per event, a random session within +-250 of the same
security (not itself an event), pseudo-level at the SAME standardised distance b, same target rule.

**Rates (H = 20).** Base 21.1%, fail 72%, neither 7%; by segment confirmed / unconfirmed: N20 26.2 / 19.4, N55 26.2 /
20.0, up 25.5 / 18.8, down 27.1 / 20.6, US 25.8 / 19.1, EU 27.6 / 20.4, UK 26.0 / 19.6, JP 26.0 / 20.1 (every segment
CI > 0 for the raw difference). Mean dir-signed 20-session log return: confirmed -0.28%, unconfirmed -0.13% (breakouts
do not drift in their direction on average).

**Model (purged walk-forward, 2-year test blocks 2008 -> 2026; training < block start - 425 days, isotonic calibration
year [start - 365, start - 60) days; logistic on standardised clipped features, HGB as candidate).** Full logistic ECE
1.20 pp, Brier 0.1603 (geometry-only 0.1610, climatology 0.1651); HGB slightly better (ECE 0.84, Brier 0.1593) but not
used (same regime failure; logistic is interpretable). Predicted quintiles: 11.9 / 15.8 / 18.7 / 22.7 / 33.8% vs realised
12.9 / 17.1 / 20.0 / 22.9 / 32.0%. Standardised coefficients (latest block): the breakout-day return +0.22, distance b +0.13,
trend +0.07, relvol50 +0.06 (relvol20 -0.02), OBV confirmation +0.02, gap beyond the level +0.05 (gap size -0.09), vol
ratio -0.20. Regimes (gap realised - predicted): Covid 2020 +17.2 pp (n 34k), 2020 +6.5, 2022 +2.3, VIX > 25 +1.5,
bear +1.0, regions / direction / N / confirmed within 1.2 pp. Leakage: the same pipeline on permuted labels has Brier
0.1652 = climatology (no skill); structural causal invariance of every event feature in pytest.

## Production configuration

- `tech_factors` (nightly): every price region (US / EU / UK / JP validated; other regions computed, flagged
  "region not validated"), latest values + percentiles; the composite column stays null (composite failed). Seed: the
  research build 2004 -> (daily, ADV >= $200k or mvp).
- `tech_breakouts` / `tech_breakout_state` (nightly): events of the last 80 sessions and the latest state; P(follow-through)
  from the latest fit, gate experimental. Seed: research events (liquid) with the walk-forward OOS probabilities.
- `tech_factor_eval` (monthly): validation rows + a trailing-156-week monitor IC per factor x region; refits the
  P(follow-through) model.
- Verdicts are read from `data/reports/technicals_v3/gate.json`; re-running `wf_chain.ps1 -Steps eval,bo,econ,report`
  updates them.

## Tests and end-to-end check

- pytest `tests/techv3` (14): factor math vs hand-computed / brute-force references (52-week high, 12-1, reversal, MAX,
  IVOL vs lstsq incl. the market-only fallback, residual momentum vs an explicit 36-block lstsq loop, strided / rolling
  helpers), percentiles with a reference set and ties, the sign-aligned composite, causal invariance of every factor and
  every breakout feature (+ a peeking control), breakout detection / first passage / features on hand cases, NW / BH /
  Holm, rank IC and label permutation, and the P(follow-through) model's permutation control.
- API (`tests/test_api_serving.py::test_technicals_factors_breakout`): the fixture lake runs the REAL nightly jobs
  (`run_factors`, `run_breakouts`) and the model fit; contract validation of both endpoints; `test_all_operations_covered`
  green. Related suites (contracts, platform, backtest, modeling, techprob, API serving): all pass.
- vitest `src/api/techv3` (7 adapter tests); tsc and oxlint clean; `vite build --mode app` into a scratch outDir.
- End to end on a COPY of the live snapshot + prices (scratchpad/tech_v3_b15/e2e_data): seed jobs (30.2M factor rows,
  1.78M breakout events, 10.4k states), monthly eval job (validation + monitor rows, model refit), serving tables, a test
  API on :8768 and headless Chrome (AAPL / JPM / SAP.DE, dark + light, experimental on / off): no console errors, no
  failed requests, no horizontal overflow. Screenshots: scratchpad/tech_v3_b15/shots/. A Backtest Lab run on source
  tech_factors (mvp_us, low-IVOL quintile, monthly) completes.

## Known limitations

- Survivorship: listing coverage favours current names; current sector classification.
- Size by dollar volume, not market cap (no PIT shares outside mvp).
- Cross-sectional percentiles use the same-day liquid cross-section; thin sectors fall back to the region.
- Breakout exits in the engine are close-based; the label uses the intraday high / low for the target.
- The economic tests use monthly rebalancing only; a lower-turnover implementation (e.g. holding-period overlap, wider
  buffers) might reduce costs but was not tried, to avoid adding specifications after seeing results.

## Meta-labelling (#2)

Method: Lopez de Prado, *Advances in Financial ML*, ch. 3-4. Primary model = the existing v2 setup generator
(direction, entry, T1, invalidation; weekly point-in-time setups of the v2 gate universe, 2010 -> 2026-10) with its T2
two-barrier probabilities. Triple-barrier label: **win** = T1 before the invalidation within H sessions (the T2 event; a
same-bar touch of both = loss), **loss** = invalidation first, vertical barrier at H (H = 20 primary; 10 and 40
secondary). The **meta model** re-estimates P(win) (and P(loss)) from point-in-time context. Code
`backend/app/techmeta/` (`features.py`, `dataset.py`, `model.py`, `job.py`, `serve.py`, `leakage.py`,
`backtest_hooks.py`, `state.py`). Scripts `scripts/technicals_v3/meta/` (`build_dataset.py`, `add_extra.py`,
`wf_meta.py`, `leakage_meta.py`, `econ_meta.py`, `report_meta.py`, `meta_chain.ps1`). Raw results
`data/reports/technicals_v3/meta/` (`meta_oos_h{10,20,40}.parquet` OOS predictions, `wf_h*.json` per-year fit records
incl. hyperparameters / calibrator / threshold / ablation / tests, `leakage.json`, `econ.json` + `econ_returns.parquet`,
`summary.json` + `summary.md`, `gate.json` = the verdicts the weekly fit copies to `tech_meta_gates`,
`importance*.json`, `reliability_meta_h*.svg`; `r2_noextra/` = the same run without the #1 / #5 features).
Everything is MODEL-BASED: "calibrated on history", never "X% chance today".

### Verdict

| Output | ECE win / loss (<= 2.5 pp) | Worst regime (<= 5 pp) | Brier meta / T2 / context-free | log-loss meta / T2 / context-free | Brier meta - T2 [95% CI] | Brier meta - context-free [95% CI] | Gate |
|---|---|---|---|---|---|---|---|
| **Meta P(win), H = 10** | 0.50 / 0.78 | Covid 2020 win -4.1 / loss +2.5 (T2 alone: loss **+5.1**) | 0.16777 / 0.16817 / 0.16799 | 0.50738 / 0.50873 / 0.50777 | **-0.00040 [-0.00071, -0.00006]** | -0.00022 [-0.00050, +0.00013] | **PASS** |
| Meta P(win), H = 20 | 0.60 / 0.85 | Covid 2020 win -4.7 / loss +2.3 | 0.16891 / 0.16900 / 0.16888 | 0.50880 / 0.51014 / 0.50872 | -0.00009 [-0.00047, +0.00033] | +0.00003 [-0.00032, +0.00037] | **FAIL** (not better than T2 / context-free) |
| Meta P(win), H = 40 | 0.50 / 0.50 | Covid 2020 3.8 | 0.16951 / 0.17023 / 0.16928 | 0.50931 / 0.51342 / 0.50893 | -0.00072 [-0.00125, -0.00023] | +0.00023 [-0.00022, +0.00067] | **FAIL** (the context-free model is better) |
| Take / skip + size, H = 10 / 20 / 40 | | | | | | | **FAIL** (economic check, below) |

Label permutation: no skill (mean Brier skill vs climatology +0.0004 LR / +0.0009 XGB over 14 years). Shifted-feature
peek: caught (log-loss -0.11 / -0.16, the peeking column dominates permutation importance). Structural leakage: pass on
4 securities x 2 cuts, both deliberate peeks caught (bars and as-of joins).

**Reading.**
- **The meta model is well calibrated** at every horizon (ECE 0.5-0.6 pp vs T2 0.9-2.0 pp) and fixes T2's
  calibration-in-the-large in stress regimes (H = 10: Covid loss gap +5.1 -> +2.5 pp; 2022 +3.7 -> +1.7; VIX > 25
  +3.5 -> +0.5). It is significantly better than T2 alone at H = 10 and H = 40 (log-loss also at 20).
- **But the context adds almost nothing beyond recalibration.** Most of the gain over T2 is the context-free logistic
  re-calibration of T2 (T2 + geometry + setup descriptors). Meta - context-free is not significant at any horizon
  (H = 10 -0.00022, CI includes 0; H = 20 +0.00003; H = 40 +0.00023, i.e. the context-free model is *better*). The H = 10
  PASS therefore means "a better-calibrated P(win) than T2", not "context has a demonstrated edge".
- **Permuting the context** (all context columns shuffled jointly, model refitted) costs nothing on average at H = 20:
  log-loss of the real-context model minus the permuted-context model -0.0007 +- 0.0006 (LR) / -0.0011 +- 0.0010
  (XGB) - i.e. the permuted version is, if anything, slightly better; the real context helped in only 5 / 7 of 14 years.
- **Which context features matter** (H = 20, drop-one-group refits, 14 test years; mean change of OOS log-loss x 1e4
  when the group is removed, +- se; positive = the group helped):

  | Group | LR | XGB | Permutation importance LR / XGB (x 1e4) | Production fit mean \|contribution\| (logit, H = 20 XGB) |
  |---|---|---|---|---|
  | market (VIX level / pct / change, risk gauge, W4 states, correlation) | -5.4 +- 4.8 | -10.1 +- 9.9 | 2.5 / 8.7 | 0.31 (largest) |
  | techctx (squeeze percentile, ADX, DI, trend alignment, ATR / RV pct) | -1.0 +- 1.2 | +1.1 +- 1.3 | 2.2 / 1.4 | 0.06 |
  | smart (insider 90d, short interest, smart-money score) | -1.1 +- 1.0 | -0.8 +- 1.2 | 0.9 / -0.3 | 0.03 |
  | earnings (days to / since, post-earnings) | -0.5 +- 0.2 | -0.7 +- 1.5 | 0.3 / 0.1 | 0.01 |
  | peer (conformal break-away z, group anomaly ratio) | -0.1 +- 0.1 | +3.6 +- 3.5 | -0.1 / 0.0 | 0.00 |
  | sector / region | +0.0 +- 0.3 | -1.7 +- 1.2 | 0.6 / 0.0 | 0.00 |
  | **#1 factors + #5 breakouts** (round 3 vs round 2, same rows) | | | | 0.05 |

  The market group carries the largest weights (the models do use VIX / risk-gauge states) but removing it *improves*
  OOS log-loss on average: its effect is not stable from year to year (sign flips, e.g. 2018). No group has a
  significant positive contribution. **#1 / #5 features** (hi52 / MAX / IVOL percentiles raw and direction-signed; the
  latest 20 / 55-session breakout within 7 days: alignment, confirmed flag, confirmation score, relative volume, days
  since): Brier with - without, same OOS rows: H = 10 +0.00000 [-0.00009, +0.00009], H = 20 +0.00002 [-0.00010,
  +0.00014], H = 40 **-0.00049 [-0.00088, -0.00013]** (helps at 40 sessions, where hi52 / IVOL carry most of the
  weight; not enough to beat the context-free model). Mean |contribution| of the extras in the production fit: 0.14 (H =
  10 LR: breakout alignment, hi52, MAX), 0.05 (H = 20), 0.01 (H = 40). Kept for the SHAP / driver explanations of
  Bundle C #12 (`importance.json`, `importance_with_extras.json`, `wf_h20.json` per-year permutation importance).
- **Expected R is well ordered** (corr(predicted, realised R) 0.45-0.47; deciles monotone) but the top decile is
  optimistic (H = 20: predicted +0.11 R, realised +0.03 R), as for T2.

### Economics (Backtest Lab engine, costs on)

Setup-following strategy as in the v2 economic check (entry next close, close-based exit at T1 / the invalidation or
after H sessions; long setups; US 100 x 1%, EU 50 x 2%, UK 15 x 6.7%, JP 20 x 5%), 2013-01 -> 2026-10, walk-forward OOS
predictions. **62 portfolio configurations** (4 regions x 3 horizons x 5 variants + 2 US long / short).

| Config (US, H = 20) | Sharpe | CAGR | Max DD | Avg gross | Trades | Hit rate | Turnover (x / yr) | DSR |
|---|---|---|---|---|---|---|---|---|
| all setups (primary as-is) | 0.98 | 13.3% | -18.2% | 0.73 | 37,322 | 70.3% | 54.2 | 0.92 |
| T2 E[R] > 0 (v2 filter) | 1.00 | 14.0% | -17.6% | 0.79 | 31,702 | 61.5% | 46.0 | 0.92 |
| context-free take (E[R] > 0) | 0.95 | 13.0% | -18.4% | 0.76 | 33,459 | 65.3% | 48.6 | 0.88 |
| **meta-filtered** (meta E[R] > in-sample threshold) | 0.77 | 8.7% | -12.5% | 0.48 | 18,961 | 58.5% | 27.5 | 0.79 |
| **meta-sized** (taken, quarter-Kelly relative size) | 0.79 | 8.9% | -14.8% | 0.44 | 18,471 | 58.3% | 25.8 | 0.82 |
| all setups, long + short | -0.10 | 0.7% | -28.0% | 0.74 | 36,601 | 65.7% | 53.1 | 0.01 |
| meta-filtered, long + short | 0.81 | 9.0% | -12.0% | 0.48 | 18,994 | 58.6% | 27.6 | 0.83 |

Paired daily-return differences (annualised, t): meta-filtered - all **US -4.3% (t -2.55)** at H = 20, -5.3% (t -2.62)
at 10, -1.9% (t -1.71) at 40; EU -4.3% (-1.62), UK -4.8% (-1.76), JP -5.9% (-1.92) at 20; meta-sized - all US -4.2%
(t -2.41); meta-filtered - T2 filter -5.0% (t -2.88). The only positive comparison is long + short (meta-filtered +8.0%/yr,
t 2.89, vs taking every short setup, which loses money in a rising market - the meta model mostly skips the shorts).
By year (US H = 20, all vs meta-filtered): the filter lagged in 10 of 14 years and led in 2014, 2022 (-0.7% vs
-6.4%), 2024 and 2026. PBO over the 62 configurations 0.35 (0.48 within US H = 20); DSR does not discriminate (0.79-0.96
for the long-only US configs).

**Reading.** At the trade level the decision has information: taken setups realised +0.008 R (H = 20) vs -0.058 R
for skipped ones (weekly t of taken - all +3.5; H = 10 / 40: +5.3 / +7.0). But only 12-19% of the setups are taken, so
the portfolio holds about half the gross exposure of "all setups", and in a long-only equity book over 2013-2026 lower
exposure costs more than the better per-trade selection earns (mean trade return 0.65% vs 0.55%). Exposure-matched
(return per unit of average gross) the filter is roughly break-even. **Null economic result: take / skip and the
suggested size fail** and are shown only as experimental.

### Setup

- **Universe / data:** the v2 T2 walk-forward rows (mvp US 490 + EU 90 + UK 27 + JP 40 names), 505,879 setups at H = 20
  (507k / 503k at 10 / 40), OOS T2 probabilities from `data/reports/technicals_v2/t2_oos.parquet` (surfaces fitted on
  quarters < Q(Y) - 1). Test years 2013-2026 (424k OOS rows at H = 20).
- **Features (point in time, 89 with the extras):** T2 base (logit P(win), logit P(loss), P(neither), E[R] marked to
  market, costs in R); geometry in sigma units (log a, log b, R:R, direction, limit-entry gap, annualised vol, vol regime,
  earnings inside H); setup descriptors (trend / structure / location / momentum sub-scores, trend and structure signed);
  techctx (`app.technicals.context` on bars <= the origin: range-width percentile = squeeze, realised-vol and ATR
  percentiles, ADX, +DI vs -DI, daily / weekly trend alignment, distance to SMA50, 5 / 20-session returns, signed);
  market (FRED VIX level / 1y percentile / 20d change; cross-asset risk gauge by knowledge date + 20d change, first
  eigenvalue share; W4 cycle quadrant / HMM regime / central-bank rate cycle **lagged two months**); peer (P4 GCN-GRU
  walk-forward break-aways: signed member z, group score / 90% radius, days since; NaN outside a peer group); smart
  (Form 4 open-market insider net buying 90d by knowledge date, buyers / sellers, cluster; FINRA short interest % / z /
  days to cover; smart-money composite; NaN outside source coverage: insider from 2015-16, SI and the composite from 2023);
  earnings (sessions to / since, post-earnings window; realised dates as pre-announced, as in v2); region / sector; #1 /
  #5 extras (round 3). **Not used:** news sentiment and event exposure (history starts 2025-10 / 2026-10: no testable
  walk-forward), CFTC (not relevant for single stocks).
- **Protocol (per test year Y and horizon):** fit block = origins < 1 Jan Y-1 - embargo (embargo 1.5 H sessions in
  calendar days + 7: 28 / 49 / 91 days >= H); inner validation = the fit block's last year, inner training ends one more
  embargo earlier -> hyperparameters (LR L2 in {1e-4 .. 1}; XGB depth 2 / 3, min child weight 300, early-stopped trees);
  calibration block = [1 Jan Y-1, 1 Jan Y - embargo) -> calibrator (identity / Platt / isotonic on its held-out half),
  LR vs XGB choice (held-out half), take threshold (grid -inf / -0.05 / 0 / 0.025 / 0.05 / 0.1 maximising the weekly t of
  realised R), Kelly size reference. Both learners use the T2 logit as an **offset** (LR) / **base margin** (XGB), so
  "no usable context" reproduces T2. Sample weights = average uniqueness per security (mean 0.77 at H = 20). XGBoost CPU
  hist, nthread 4; peak RAM 2.8 GB.
- **Gate (stated before results):** ECE <= 2.5 pp (win and loss); |realised - predicted| <= 5 pp in Covid 2020, 2020,
  2022, VIX > 25 and every region / side / vol regime / earnings window with n >= 2000; Brier and log-loss below T2
  alone and the context-free logistic model, and the month-block CI of Brier(meta) - Brier(T2) below 0; label
  permutation BSS <= 0.002; shifted-feature peek caught; structural leakage tests pass. Decision outputs additionally
  need US meta-filtered - all setups paired t >= 2 and no other region t < -1.
- **Multiple testing:** 3 horizons x 5 OOS methods (T2, context-free, LR, XGB, selected) = 15 OOS evaluations per round;
  3 rounds - round 1 (H = 20, stopped after 3 test years: a permutation-importance bug and a P(loss) renormalisation that
  degraded P(win); fixed, identity calibration and stronger L2 added), round 2 (no extras, complete), round 3 (with the
  #1 / #5 features, complete; verdicts identical to round 2) -> 36 complete OOS method x horizon evaluations; inner
  candidates per fit 12 hyperparameter settings x 3 calibrators x 6 thresholds (chosen in-sample); 6 ablation groups x 2
  learners; 62 portfolio configurations. With 3 horizons tested the H = 10 pass is modest evidence: its Bonferroni (98.3%)
  interval is [-0.00081, -0.000002], excluding 0 only barely.

### Production configuration

- `tech_meta_fit` (weekly, after `technicals_probabilities_fit`): appends completed origins of `tech_prob_setup_events`
  (features built by the same code as the walk-forward; T2 from the latest surface), refits per H on everything before
  fit date - 1y - embargo, calibrates / chooses / thresholds on the last year, writes
  `data/models/technicals_v3/meta/<fit>/meta_model.pkl` and copies `gate.json` into `tech_meta_gates`. The #1 / #5
  features are used only when their nightly tables (`tech_factors`, `tech_breakouts`) are fed (else left out: no train /
  serve skew; their walk-forward value was ~0 at 10 / 20 sessions anyway).
- `tech_meta_score` (nightly): every T2 setup (~20.8k securities, 62k rows, ~6 min, ~1.9 GB) -> `tech_meta_setups`;
  rows outside the validated domain (ETFs / indices, regions other than US / EU / UK / JP) are always experimental.
- UI: the Trade setup card's "Context-adjusted (meta)" row and the thesis "Target before stop" meta table show **H = 10
  by default** (passed); 20 / 40 sit behind the experimental toggle; take / skip and size are hidden at every horizon
  unless the toggle is on. Backtest Lab: event trigger `meta_setups` (all / take / sized), notes carry the verdict.

### Known limitations

- Survivorship (current mvp membership) and current sectors, as in v2; peer groups are the current version.
- W4 macro states use today's vintages and parameters (lagged two months here, but not a true real-time history).
- Insider / short-interest / smart-money history is short (2015-16 / 2023 ->): their effect is estimated from few years.
- The engine's exits are close-based while the labels use intraday highs / lows (as in v2).
