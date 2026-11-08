# Technicals v2: presentation + models (plan)

**Status:** plan; waiting for the user's OK.
**Scope:** the Company 360 Technicals tab and the models behind it.
**Builds on:** conformal envelopes and touch curves (P1–P4), tech_levels, confluence_zones, level_type_backtest, technical score.

## Problems seen on the current tab (AAPL, 2026-10-21)
1. **The cone is unreadable.** The chart's time axis ends at the last bar, so the 1/5/10/20-session bands collapse into a vertical spike at the right edge. Only one level (90%) shows, as two thin lines.
2. **Duplicated and wasted space.** The "Calibrated range" table repeats the cone, and there is about 400 px of empty space below the chart.
3. **The trade setup has no probabilities.** Entry, T1 and invalidation come with R:R 1.45 but no chance of reaching T1 before invalidation, and no expected value.
4. **Scores overstate.** Technical sub-scores saturate at 100 (trend, structure, momentum). Zone "strength 76" looks decisive, yet level-type bounce edges against random levels are only +0.3 to +1.4 pp, and the page never says so.
5. **No context for the range.** Nothing shows whether today's range is narrow or wide relative to the stock's own history (squeeze vs expansion), or how it relates to the earnings date.

## Proposal

### T1. Cone redesign (presentation)
- **Future space:** extend the time axis 20 sessions into the future using real exchange business days, with the earnings date marked when it falls inside.
- **Nested fan:** 95/90/80 drawn as an opacity gradient, with a dashed zero-drift centre line. No direction call is made, so the centre is flat.
- **Continuous horizons:** compute the calibrated band for **every horizon from 1 to 20**, not only 1/5/10/20. This is a cheap extension of the envelope job using the variance term structure plus pooled calibration per horizon. The cone is then smooth, and a crosshair in the future area shows "by Nov 6: 90% range 318–371".
- **Zone badges:** at the right edge of each confluence zone inside the cone, a small badge with its touch probability for the selected horizon.
- **Merged table:** the separate table becomes a compact strip under the chart (1d/5d/10d/20d, low/high, % move). The calibration wording moves into a single info popover.
- **Layout:** the chart fills the panel height, with no dead space. Zone labels are decluttered: show the top 3 zones by default and the rest on hover.

### T2. Probabilistic trade setup (model)
- **Two-barrier first-passage model:** P(T1 reached before invalidation within H sessions), calibrated empirically on history in the same way as the touch curves. It is a path event, and not Gaussian.
  - Conditioning: distance to each barrier in volatility units and the regime pool.
- **Outputs:**
  - **P(win)**, **P(loss)** and **P(neither by H)**;
  - **expected R multiple** = P(win)·reward − P(loss)·risk, after costs;
  - **break-even probability**, so the user sees whether 1.45 R:R is enough.
- **Validation:** a reliability plot (predicted vs realised) in walk-forward, plus a comparison against the naive "R:R only" rule.
- **Display:** shown on the Trade setup card and in the Thesis tracker, which reuses the same endpoint.

### T3. Zone hold/break model (model)
- For each zone: **P(touch within H)**, which already exists, × **P(hold | touch)**.
- **P(hold | touch):** a calibrated classifier (logistic / gradient boosting with isotonic calibration, purged CV). Inputs:
  - level types present and their count (confluence);
  - zone width in volatility units;
  - approach speed;
  - trend and regime;
  - prior touches;
  - volume profile;
  - distance to the moving averages.
- **Honest baseline:** the same model on **random control levels**, with the **edge vs random** shown per zone. If the edge is not significant, the UI says "no reliable edge".
- **Replaces the opaque "strength":** strength becomes a calibrated "hold probability" with its uncertainty. The old score is kept under a details toggle.

### T4. Calibrated technical score (model)
- **Fix saturation:** sub-scores become percentile ranks within region × sector, so no more piles of 100s.
- **Map score to outcomes:** the composite technical score is translated into **historical outcomes** from the walk-forward. For example: "scores ≥ 85 → next-20d excess return median +x%, hit rate y%, n = …, CI".
- **No edge, no badge:** if the score has no out-of-sample edge, the badge says so rather than implying one.

### T5. Volatility and regime context (model + presentation)
- **Range-width percentile:** today's calibrated 20d range width against the stock's own last 2 years. A **squeeze** is ≤ 10th percentile; **expansion** is ≥ 90th.
- **Context chips:** realised and implied vol where available, ATR percentile, ADX trend strength, and trend alignment between daily and weekly. All shown as compact chips with tooltips.
- **Earnings context:** an "Earnings in N sessions: range includes the event pool" chip, plus the typical earnings move from the earnings reactions data.

### T6. Validation and QA
- **Gate:** every new probability (T2 first-passage, T3 hold, T4 score mapping) needs a walk-forward reliability check, with an ECE (expected calibration error) threshold and coverage by regime. It also needs a comparison against a naive baseline, using the same gate discipline as P4. Anything that fails is shown as "experimental" or hidden.
- **QA:** smoke routes for the redesigned tab, plus golden checks of cone geometry (monotone widening, nested levels).

## Order and effort
1. **T1** (cone + layout): UI plus a small backend extension for the 1..20 horizons.
2. **T2** (first-passage setup probabilities).
3. **T5** (context chips).
4. **T3** (zone hold model).
5. **T4** (score calibration).

Each step ships behind its own validation. T1 and T5 are mostly presentation; T2–T4 are models gated by T6.

## Progress
- [x] T1 cone + layout (2026-10-22)
  - backend: `conformal_envelopes` v2 writes fan_v + fan_qm_80/90/95 for h = 1..20 (per-h pooled calibration; walk-forward on target at every h, docs/CONFORMAL_VALIDATION.md A.5, scripts/conformal/fan_check.py); API v1.33.0 `fan` / `fan_info` on GET /conformal/envelope/{id} (exchange sessions from the O1 calendar cache, earnings date flagged; interpolated fallback for older rows)
  - UI: nested 95/90/80 fan over 20 future sessions with a flat dashed centre, future-area tooltip, earnings marker, zone touch badges (1/5/10/20d), top-3 zones + hover preview, compact range strip + one calibration popover, chart fills its panel; slots for T2/T3/T4 in `frontend/src/pages/company/tabs/TechnicalsSlots.tsx`
- [x] T2 first-passage setup probabilities (2026-10-22): empirical two-barrier grid surface (app/techprob/barriers.py), GET /technicals/{id}/setup-probabilities (v1.34.0, + thesis target/stop); gate: **H = 20 PASS**, H = 5/10 FAIL (Covid 2020 window > 5 pp), H = 40/60 FAIL (not better than the Brownian baseline) -> experimental. Expected-R filter: economic null result
- [x] T5 volatility/regime/earnings context (2026-10-22): GET /technicals/{security_id}/context (v1.33.0; app/technicals/context.py) + chips row above the chart (range-width squeeze / expansion, RV 20d, IV n/a, ATR, ADX, D/W trend alignment, earnings in N sessions + typical |CAR[-1,+1]|), each with a tooltip
- [x] T3 zone hold/break model (2026-10-22): P(hold | touch) hgb + isotonic and random-control pipeline (app/techprob/zone_model.py), GET /technicals/{id}/zones; gate: P(hold) FAIL (Covid 2020 -8.7 pp) -> experimental; per-zone edge flag PASS. On average zones hold 0.6 pp LESS often than random bands; top quintile +2.1 pp
- [x] T4 calibrated technical score (2026-10-22): percentile sub-scores within region x sector + score-bucket outcomes, GET /technicals/{id}/score-calibration; gate FAIL (no out-of-sample edge: rank IC -0.003..-0.014, n.s.) -> bucket stats experimental, card says so
- [x] T6 validation + QA (2026-10-22): docs/TECHNICALS_V2_VALIDATION.md, data/reports/technicals_v2/ (walk-forward 2010-2026, leakage, reliability, regimes, baselines, Backtest Lab economics)
