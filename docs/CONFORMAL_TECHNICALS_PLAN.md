# Conformal Ellipsoids for Technical Prediction (plan)

**Source:** Y. Limmer (DRW), *Filtered Conformal Ellipsoids for Graph-Native Time Series*, arXiv:2606.17014v2 (25 Sep 2026). The paper says its code is MIT-licensed.
**Status:** approved 2026-10-21; P1, P2, P4 and P3 done (see Progress and `docs/CONFORMAL_VALIDATION.md`); live after a serving rebuild + API / worker restart.

## 1. What the paper gives us, in one paragraph
A frozen **filter** (a model trained once, then fixed) emits, before Y_t is observed, a one-step predictive mean Ŷ and covariance Σ̂ for a vector Y_t ∈ R^N. On a chronological calibration block we compute squared Mahalanobis scores R = (Y−Ŷ)ᵀΣ̂⁻¹(Y−Ŷ) and take their empirical (1−α) quantile q̂ (the split-conformal step). The prediction set is the **ellipsoid** {y : (y−Ŷ)ᵀΣ̂⁻¹(y−Ŷ) ≤ q̂}:
- the filter decides the **shape**: orientation and per-axis scale, i.e. cross-asset dependence;
- the conformal step decides the **radius** from observed residuals, not from Gaussian tail assumptions.

Properties we exploit:
- **Joint coverage:** one event "all N coordinates inside" with probability ≈ 1−α, instead of N separate intervals.
- **Closed-form projection to any linear combination:** aᵀY ∈ aᵀŶ ± √(q̂·aᵀΣ̂a). Any single price, horizon, spread, pair or portfolio gets a calibrated band from the same set.
- **Anomaly score:** R_t > q̂ means the joint move was outside the calibrated region. The score's percentile is a calibrated measure of how unusual a move is.
- **Under dependence and drift** (which financial data has), coverage is approximately marginal (Theorem 7). An **ACI wrapper** (adaptive conformal inference: the radius adjusts online) restores at-target coverage when the regime drifts (§6.4).
- **Sweet spot:** moderate N (about 20–50) with a meaningful graph. A learned GCN-GRU filter (a graph neural network with a recurrent cell) whose covariance is a diagonal plus a rank-4 term beat static and factor covariances there by 24–40% in sharpness. At large N, factor-covariance baselines can win.

**Why this fits finance well:** return *means* are barely predictable, but return *covariance and volatility* are. The method puts its effort exactly where finance has signal (the shape) and stays honest about the size of the region (the calibrated radius).

## 2. How it plugs into "any technical prediction"
Today our technical layer (tech_levels, confluence_zones, the technical score, level_type_backtest) gives **point levels and heuristic bands**: ATR-style ranges and zone strengths. The plan wraps every technical prediction in a **calibrated uncertainty set**.

| # | Application | Y vector (N) | Graph / shape | What the user gets |
|---|---|---|---|---|
| A | **Conformal price envelope** per security | forward log-returns at horizons 1/5/10/20 d, or next-bar (high, low, close) vs. previous close (N = 3–4) | none; the filter is EWMA/GARCH-style, plus a small learned head | Calibrated 80/90/95% bands per horizon on every chart (replacing ATR bands); the next-bar high–low range; a **probability-of-touch for every confluence level and thesis target/stop** via projection |
| B | **Peer-group joint ellipsoid** (graph-native, the paper's sweet spot) | next-day returns of an industry/peer basket (N ≈ 20–50) | supply-chain links + statistical neighbours + same-industry adjacency (existing linkage tables) | A daily **joint-anomaly score** per group (a "the group broke out together" vs. "one name broke away" signal); a per-name contribution; **calibrated spread bands for any pair or relative-value trade** (aᵀY with a = e_i − β·e_j) |
| C | **Cross-asset ellipsoid** | weekly returns of the 12-asset regime basket, optionally the 73 cockpit assets | correlation / factor graph | A calibrated **joint stress score** feeding the risk-on/off gauge; bands for any cross-asset spread (e.g. copper/gold, 2s10s) |
| D | **Yield-curve ellipsoid** | daily changes of the curve tenors | adjacent-tenor chain graph | Calibrated curve-move bands, and the 2s10s/5s30s band in closed form |
| E | **Portfolio / watchlist band** | the basket from B or C | (same filter) | A calibrated range for any watchlist or backtest portfolio return (aᵀY with the weights), a conformal VaR-like figure |

**Downstream integrations:**
- **Technicals:**
  - every support/resistance or confluence zone gets *P(touch within h days)* and *inside or outside the 90% envelope*;
  - breakouts are flagged only when the move exits the calibrated band, which cuts noise breakouts.
- **Backtest Lab:** new signals.
  - (i) **Breakout:** R_t > q̂, or the price exits its projection band.
  - (ii) **Mean reversion inside the band.**
  - (iii) **Calibrated stop/target placement:** the stop sits at the lower band of a chosen α instead of a fixed ATR multiple.
- **Thesis tracker:** suggested entry/target/stop from the envelope, plus the live probability that the target or stop is reached within the horizon.
- **Alerts:** "joint anomaly in peer group X", and "price outside the 95% envelope".
- **Model Studio:** a new method, "Filtered conformal ellipsoid", usable on *any* multivariate series (macro panels, CFTC, curves), with exportable code.

## 3. Build plan (phases)

**P1: Core library `backend/app/conformal/`** (no UI)
- `scores.py`: squared Mahalanobis via a Woodbury identity for diagonal-plus-low-rank Σ (O(Nr²+r³)); the split-conformal quantile ⌈(n+1)(1−α)⌉; projections aᵀY; the w_proj metric and log-volume.
- `aci.py`: ACI / AgACI online radius adaptation (Gibbs–Candès), which is the drift guard for finance.
- **Filters**, all emitting (Ŷ, Σ̂) before Y_t is seen:
  - (1) EWMA/RiskMetrics mean + covariance;
  - (2) shrinkage static covariance (the baseline);
  - (3) PCA-factor covariance;
  - (4) DCC-lite / GARCH-diagonal;
  - (5) **learned GCN-GRU** with a diagonal + rank-r head (torch CPU, already installed), trained by Gaussian NLL with teacher forcing and the paper's ε floor of 1e-4.
- **Protocol:** chronological pretrain / calibration / test, the filter frozen after pretraining, warm-up discard t₀ ≥ 50, rolling re-calibration. Reuse the purged-CV and leakage harness.
- **Tests:**
  - the exact-validity case (Proposition 8: i.i.d. standardised innovations give coverage within [1−α, 1−α+1/(n+1)]);
  - the projection formula; Woodbury against dense;
  - an ACI drift test;
  - a shuffled-time leakage check.

**P2: Applications A + B** (the highest value)
- **Job `conformal_envelopes`:** nightly; EWMA filter for the full universe, learned head for mvp (781). Writes a compact table per security per day: Ŷ, the Σ̂ factors, q̂ at 80/90/95, the ACI state and the realised score.
- **Job `conformal_peer_groups`:** weekly fit, daily scoring. About 150–300 industry/peer groups of N = 20–50, built from the linkage graph and statistical neighbours.
- **Coverage audit table:** realised joint and per-horizon coverage per region, sector and regime, compared with the target. This feeds Data Health and the QA guard.

**P3: Integrations**
- API (additive endpoints): envelope per security, touch probabilities for levels, peer-group anomaly, pair band.
- UI:
  - chart band overlay plus a probability-of-touch column in the levels table (Company 360 Technicals);
  - a peer-group anomaly panel (Sectors and Company 360);
  - a pair/spread band tool.
- Backtest Lab signals and stop/target mode; thesis tracker suggestions; the two alert types.
- A Model Studio method entry.

**P4: Validation gate** (must pass before anything is shown as more than "experimental")
- **Walk-forward 2010→2026:**
  - joint coverage within ±1.5 pp of target at α = 0.1 and 0.05 overall, with no regime (2020 Covid, 2022 rates shock) more than 5 pp under;
  - sharper than the baselines: the ATR band, univariate conformal, static covariance and factor covariance, measured by w_proj and log-volume;
  - comparisons are against these baselines, not against nominal Gaussian bands.
- **Economic value in the Backtest Lab** (costs included, deflated Sharpe and PBO reported):
  - breakout-on-exit vs. a naive breakout;
  - band-based stops vs. ATR stops.
- **Labelling:** everything is labelled **model-based**. Coverage is *marginal* (over time), **not** conditional on any one day. The UI must say "calibrated to X% over history", never "X% chance today".

## 4. Honest risks
- **Heavy tails and regime breaks** violate exchangeability. ACI and rolling re-calibration are mandatory, and coverage is audited per regime.
- **Means are near zero,** so the value is in the shape and the calibrated size, not in direction calls. The envelope does not predict direction.
- **Joint sets grow conservative as N rises,** which is why the plan keeps groups at N ≈ 20–50 per the paper. At large N, use factor covariance.
- **Overnight gaps and earnings days:** these are scored separately or flagged, so earnings jumps don't inflate normal-day bands. We have the earnings calendar from item #6.
- **Compute:** the EWMA filter is trivial. The learned GCN-GRU is only for mvp and peer groups, trained weekly on CPU (minutes per group); RAM stays within the app's governor.

## 5. Decisions for the user (defaults recommended)
1. **Scope order:** P1 → A + B (+ Backtest/thesis/alerts integration) → C, D, E later. *(recommended)*
2. **Coverage levels shown:** 80% / 90% / 95%, with 90% as the default.
3. **Horizons:** 1, 5, 10 and 20 trading days, plus the next-bar high/low/close.
4. **Learned filter universe:** mvp (781) plus peer groups. The full universe uses the EWMA filter.
5. **Gate:** show in the UI only after P4 passes; until then, Model Studio and Backtest only, as "experimental".

## Progress
State file: `data/state/conformal.json` (resume point, last job reports).
- [x] P1 core library + tests (`backend/app/conformal/`, `tests/conformal/test_core.py`)
  - [x] scores.py: dense + Woodbury Mahalanobis / log-det, conformal quantile, projections, w_proj, log-volume, attribution
  - [x] aci.py: ACI (delayed + pooled feedback), AgACI
  - [x] filters.py: EWMA, Ledoit-Wolf static, PCA factor, GARCH-diag / CCC / DCC-lite; learned.py: GCN-GRU, horizon GRU
  - [x] protocol.py (Algorithm 1, rolling recalibration, ACI wrap), leakage.py (causal invariance + shift test)
  - [x] tests: Proposition 8 oracle validity, Woodbury = dense, projection = support function, ACI regime shift, leakage, affine invariance
- [x] P2 envelopes (A) + peer groups (B) + coverage audit
  - [x] contracts: conformal_envelopes, conformal_touch_curves, conformal_peer_groups, conformal_peer_scores, conformal_peer_members, conformal_coverage
  - [x] A: envelope.py + envelope_job.py (job conformal_envelopes, nightly), touch-probability curves, earnings / gap handling
  - [x] B: peer_groups.py (group builder v20261020, nightly scoring conformal_peer_groups, weekly fit conformal_peer_groups_fit)
  - [x] coverage.py (job conformal_coverage) + O1 registry (coverage.yaml analytics.conformal_*, check conformal_coverage in app.ops.gaps)
  - [x] jobs.yaml v31 (worker restart needed to pick them up)
  - [x] production configuration applied after P4 (A: EWMA, plain joint/marginal radius, ACI next bar; B: GCN-GRU + ACI), backfills re-run
- [x] P3 API/UI/Backtest/Thesis/Alerts/Studio integration (scope after P4: B descriptive panel + pair bands; A only qm_* calibrated bands and touch probabilities, labelled)
  - [x] serving: `_Builder.conformal()` -> cf_envelopes (latest origin per security, marginal qm_* + shape + next-bar; no joint b{h}_*), cf_touch_curves, cf_peer_groups (latest version), cf_peer_scores (last year), cf_peer_members (last 127 sessions, float32: attribution + laws for the marginal pair-band calibration), cf_coverage (latest audit per application); serving_sections += conformal, conformal_peers; fixture tests/fixtures/conformal_fixture.py (via serving_small)
  - [x] API v1.32.0 (router app/api/routers/conformal.py, helpers app/conformal/serve.py): GET /conformal/envelope/{id}, /conformal/touch (given levels or auto: zones + technical levels; 1-20 sessions validated, 21-252 extrapolated + flagged), /conformal/peer-groups, /conformal/pair-band (joint-projection band + the combination's own marginal split-conformal band), /conformal/coverage (audit + gate verdicts + production config + report)
  - [x] UI: Company 360 Technicals (calibrated-range cone 80/90/95 with toggle, calibrated-range table, calibration popover, chance-of-touch column 1/5/10/20 d on the confluence zones), peer-group panel (Company 360 Peers tab + Sector Explorer) with sparkline, member contributions, break-away member and pair / basket band tool, Thesis tracker (chance of touching target / stop; optional band hints in the editor), System > Conformal validation page, Data Health coverage panel
  - [x] alerts: price_outside_calibrated_range, conformal_peer_anomaly (evaluators, API validation, alerts.yaml v7, Watchlist metrics)
  - [x] Backtest Lab: experimental triggers calibrated_band_breakout / peer_breakaway and stop mode calibrated_band (app/conformal/backtest_hooks.py: walk-forward bands on the panel prices), labelled experimental with a link to the validation page
  - [x] Model Studio: method conformal_ellipsoid (timeseries or one panel variable across entities) + Python export (embedded app/modeling/_conformal.py) + preset conformal_macro_ellipsoid
  - [x] jobs.yaml v32: build_serving_db soft-depends on conformal_envelopes / conformal_peer_groups
- [x] P4 validation gate + report (`docs/CONFORMAL_VALIDATION.md`, raw results `data/reports/conformal/`)
  - [x] A walk-forward (scripts/conformal/wf_envelopes.py): joint envelope FAIL (Covid regime, w_proj vs calibrated ATR box); next bar FAIL (w_proj); marginal bands + touch probabilities calibrated
  - [x] B walk-forward (scripts/conformal/wf_peers.py): PASS, GCN-GRU + ACI sharpest at target
  - [x] economic value (scripts/conformal/econ.py): (i) FAIL, (ii) band stops > ATR stops but < no stop, (iii) FAIL (null)
  - [x] report + PASS/FAIL + recommended configuration
