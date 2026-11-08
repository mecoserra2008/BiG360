# Conformal ellipsoids: P4 validation gate

Plan: `docs/CONFORMAL_TECHNICALS_PLAN.md`. Paper: Limmer, *Filtered Conformal Ellipsoids for Graph-Native Time Series*
(arXiv:2606.17014v2). Code: `backend/app/conformal/`. Scripts: `scripts/conformal/` (`wf_envelopes.py`, `wf_peers.py`,
`econ.py`, `touch_check.py`, `report.py`). Raw results: `data/reports/conformal/` (`wf_env/`, `wf_peer/` incl.
break-away signals, `econ.json`, `econ_returns.parquet`, `touch_reliability.json`, and `summary.md`, the full set of
generated tables). The walk-forward coverage rows are also in the lake table `conformal_coverage` (applications
`envelope_wf`, `nextbar_wf`, `peer_wf`), next to the nightly rolling audit.

Everything here is **model-based**. Coverage is **marginal over time and securities**: "calibrated to X% over
history", never "X% chance today".

## Verdict

| Application | Coverage ±1.5 pp | No regime > 5 pp under | Sharper than baselines at matched coverage | Economic value | Gate |
|---|---|---|---|---|---|
| **A, joint 1/5/10/20 envelope** (ellipsoid) | PASS (89.6 / 94.5%) | **FAIL** (Covid Feb-Jun 2020: 77-78% at 90%) | **FAIL** on w_proj (+35% vs a calibrated ATR box); PASS on log-volume | (i) breakout: FAIL; (ii) band stops beat ATR stops but not "no stop" | **FAIL** |
| **A, next bar (H, L, C)** (ellipsoid) | PASS (90.0 / 95.0%) | PASS (Covid 91.2%, VIX>25 89.1%) | **FAIL** on w_proj (+43% vs calibrated ATR box); PASS on log-volume | not tested separately | **FAIL** (sharpness) |
| **B, peer-group ellipsoid** (GCN-GRU + ACI) | PASS (90.4 / 95.2%) | PASS (worst: Covid 85.3% at 90%, i.e. -4.7 pp) | PASS: -10% w_proj vs EWMA, -9% vs static Ledoit-Wolf, -13% vs PCA factor; sharper than the Bonferroni box, which also over-covers and is unbounded on 9-21% of days | (iii) break-away mean reversion: FAIL (negative Sharpe; not significantly better than a naive fade) | **PASS** as a calibrated descriptive measure (anomaly percentile, pair / basket bands). **Not** a trading signal. |

**Recommendation:**
- **A stays out of the main UI.** It remains "experimental" in Model Studio and the Backtest Lab.
- Two A by-products did validate and are candidates for P3 in place of the heuristic ATR bands:
  - The **marginal single-horizon conformal bands** (`qm_*`): on target per horizon (89.6-90.0%), about 40% narrower than the conventional 2×ATR band, which covers 98%. They are not sharper than a conformally calibrated ATR band; the gain comes from calibration, not from the filter.
  - The **touch probabilities**: predicted equals realised within 0.2 pp at every distance and horizon (see §A.4). A Gaussian reflection formula is 5× too low in the tail.
- **B passed the statistical gate.** It can be shown as a model-based anomaly percentile and pair/spread band, without a trading-signal framing.

## Setup

- **Universe, A:**
  - mvp US (485 of the 500 have enough history) plus the mvp_global EU (90), UK (27) and JP (40) names;
  - prices 2003 → 2026-10-20; test years 2010 → 2026.
  - Survivorship: the current mvp membership is used throughout, which is a known bias. EU/JP earnings dates only exist from about 2014-2015, US from 2010.
- **Universe, B:**
  - the 259 current peer groups (version 20261020, see P2);
  - per test year, members with ≤ 2% missing / ≤ 20% zero returns over [Y-6, Y], with N ≥ 20. This gives 2,955 group-years and 707,115 group-days.
- **Protocol (Algorithm 1, with refits):**
  - Fitted filters are fit on data before Jan 1 of Y-1 and then frozen:
    - GARCH / static / factor are fit yearly;
    - the GRU / GCN-GRU are fit once per 3-year block on all securities / groups.
  - Calibration uses a rolling window of the last 500 realised scores, restricted to origins on or after Jan 1 of Y-1, so only the frozen model's scores are used.
  - Calibration is pooled per region × sector for A and per group for B, with separate earnings-event pools for A.
  - Outcome lag: 20 sessions for the joint set, 1 for next-bar / B.
  - ACI uses γ = 0.005 and restarts each year.
  - Warm-up discard: ≥ 50.
- **Regimes:**
  - "Covid 2020" = origins from 15 Feb to 30 Jun 2020;
  - "2022" = the rates shock year;
  - "VIX>25" = FRED VIXCLS > 25 at the origin.
- **Size metrics (paper eq. 3-4):**
  - **w_proj** is the mean coordinate-projection length 2·half-width, in log-return units, averaged over coordinates.
  - **log-volume** is the normalised log-volume (for boxes: the mean log of the side lengths).
  - **"unbounded"** is the share of sets with an infinite radius: ACI α_t ≤ 0, or a Bonferroni quantile index beyond the window. These sets count as covered; their widths are excluded from w_proj.

## A. Conformal price envelope (walk-forward)

### A.1 Joint 1/5/10/20-session set, target 90%
| method | radius | joint cov | per-horizon cov h1/h5/h10/h20 | worst year | Covid 2020 | 2022 | VIX>25 | earnings window | w_proj | log-vol | unbounded |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **ewma** (ellipsoid) | plain | **89.6** | 98.7/98.7/98.7/98.8 | 84.7 (2020) | 77.0 | 89.4 | 90.2 | 89.2 | 0.3134 | -1.927 | 0 |
| ewma | aci | 89.7 | 98.8/98.7/98.7/98.8 | 87.6 (2020) | 78.3 | 90.2 | 92.1 | 89.2 | 0.3291 | -1.913 | 1.1 |
| gru (learned head) | plain | 89.3 | 98.7/98.6/98.7/98.7 | 85.0 (2020) | 77.3 | 89.8 | 90.9 | 89.2 | 0.3074 | -1.942 | 0 |
| gru | aci | 89.6 | 98.7/98.7/98.7/98.8 | 87.8 (2020) | 78.6 | 90.3 | 92.9 | 89.4 | 0.3246 | -1.925 | 1.1 |
| garch | plain | 88.7 | 97.9/97.8/97.7/97.8 | 81.6 (2020) | 72.6 | 85.4 | 85.9 | 88.2 | 0.3014 | -1.955 | 0 |
| garch | aci | 89.4 | 98.0/97.9/97.9/97.9 | 85.8 (2018) | 76.8 | 88.3 | 89.6 | 88.9 | 0.3621 | -1.915 | 1.1 |
| static (4×4 Σ per security) | plain | 88.5 | 98.2/98.1/98.2/98.2 | 76.9 (2020) | 54.2 | 80.5 | 77.6 | 87.7 | 0.3197 | -1.842 | 0 |
| static | aci | 90.0 | 98.4/98.4/98.4/98.4 | 83.8 (2018) | 68.2 | 87.2 | 85.3 | 89.4 | 0.3281 | -1.829 | 2.9 |
| factor (rank 1 + diag) | plain / aci | 88.7 / 90.0 | 98.6-98.9 | 77.1 / 83.9 | 55.6 / 69.4 | 81.4 / 87.5 | 78.9 / 86.2 | 87.9 / 89.4 | 0.3565 / 0.3665 | -1.768 / -1.755 | 0 / 2.8 |
| **atr_cal** (box, conformal ATR multiple) | plain | 89.7 | 96.6/96.3/96.3/96.4 | 85.9 (2020) | 77.3 | 89.5 | 90.8 | 89.4 | **0.2319** | -1.713 | 0 |
| atr_cal | aci | 89.8 | 96.6/96.3/96.3/96.4 | 88.4 (2020) | 78.9 | 90.0 | 92.3 | 89.3 | 0.2461 | -1.696 | 0.7 |
| atr_k2 (heuristic 2×ATR×√h, uncalibrated) | fixed | 94.8 | 98.4/98.1/98.2/98.2 | 89.6 (2020) | 81.7 | 95.9 | 95.1 | 91.2 | 0.2812 | -1.505 | 0 |
| uni_ewma (per-horizon CP at α/4, Bonferroni) | plain / aci | 92.1 / 92.2 | 97.1-97.5 | 88.4 / 90.1 | 80.0 / 82.4 | 92.0 / 92.5 | 92.7 / 93.9 | 92.2 | 0.2659 / 0.2487 | -1.616 / -1.657 | 0 / 6.8 |
| uni_static | plain / aci | 91.8 / 93.0 | 96.8-97.7 | 83.7 / 88.7 | 65.9 / 78.8 | 87.1 / 91.3 | 84.8 / 90.9 | 91.5 / 92.6 | 0.2662 / 0.2602 | -1.544 / -1.562 | 0 / 6.4 |

At 95%:
- ewma: 94.5% (plain), 94.6% (ACI); w_proj 0.3775 / 0.3660; Covid 80.1 / 81.5.
- atr_cal: 94.6%; w_proj 0.2889; log-vol -1.504 vs ewma's -1.749.

Per region (90%, ewma plain): EU 89.7, JP 89.7, UK 89.5, US 89.5. By year (ewma plain): 84.7 (2020) to 94.9 (2021). Full tables are in `summary.md`.

**Reading:**
- **Coverage.** The calibrated ellipsoids are on target overall (EWMA 89.6/94.5) in every region.
- **Regimes.** Every method fails the 20-session Covid window, baselines included: the shock arrived after the origins, and a 20-session outcome lag means neither the shape nor ACI can react in time. ACI lifts the worst year from 84.7 to 87.6, but costs +5% width and gives 1-5% unbounded sets.
- **Sharpness.**
  - The ellipsoid has the smallest **volume** of all calibrated sets (-1.93 vs -1.71 for the ATR box: about 20% shorter sides in geometric mean).
  - However, its axis projections (w_proj, the paper's headline metric) are 35% **wider** than a calibrated ATR box. The four overlapping horizons are strongly correlated (ρ(Y10, Y20) ≈ 0.7); for such coordinates a box reaches joint coverage with narrower sides, while the minimum-volume ellipsoid is long along the diagonal. The ellipsoid's per-horizon projections cover about 98.7%, which makes them simultaneous bands, not 90% bands.
  - The learned GRU head is 1-2% sharper than EWMA (0.307 vs 0.313; ACI 0.325 vs 0.329) at the same coverage. That is not material, so the mvp learned head is **not** deployed.

### A.2 Marginal single-horizon bands (each horizon calibrated alone; the chart-band use case), target 90%
| method | radius | cov h1/h5/h10/h20 | width h1/h5/h10/h20 | Covid h1/h5/h10/h20 | VIX>25 h1/h20 |
|---|---|---|---|---|---|
| m_ewma (EWMA scale + jump term) | plain | 90.0/89.9/89.8/89.6 | 0.0605/0.1386/0.1979/0.2818 | 87.7/86.8/83.9/82.4 | 87.9/92.3 |
| m_ewma | aci | 90.0/89.9/89.8/89.6 | 0.0612/0.1412/0.2064/0.3112 | 89.3/88.5/85.1/83.0 | 88.6/93.2 |
| m_gru | plain | 89.8/89.7/89.6/89.5 | 0.0589/0.1361/0.1949/0.2791 | 87.7/86.8/83.8/82.4 | 88.7/93.0 |
| m_garch | plain | 89.5/89.4/89.3/89.1 | 0.0594/0.1354/0.1918/0.2705 | 83.8/82.8/80.4/78.5 | 85.5/89.0 |
| m_atr (ATR scale, conformal multiple) | plain | 90.0/89.9/89.9/89.6 | 0.0585/0.1349/0.1931/0.2755 | 85.7/85.2/83.0/82.0 | 87.9/93.1 |
| m_static | plain | 89.7/89.7/89.6/89.5 | 0.0617/0.1388/0.1950/0.2751 | 65.7/66.2/65.7/64.6 | 78.6/83.4 |
| atr_k2 (heuristic) | fixed | 98.4/98.1/98.2/98.2 | 0.1035/0.2314/0.3272/0.4628 | 96.3/94.0/90.8/88.0 | 98.0/98.6 |

What this shows:
- The conformal step turns any scale into calibrated bands:
  - a 90% band that really covers 90%;
  - about 40% narrower than the heuristic 2×ATR band, which over-covers at 98%.
- Calibrated scales give near-identical widths (±3%), so the EWMA scale has no sharpness edge over ATR14.
- The EWMA scale stays closer to target in the Covid window at 1-5 sessions (with ACI: 89.3/88.5 vs 85.7/85.2 for ATR).

### A.3 Next bar (log H/C₋₁, log L/C₋₁, log C/C₋₁), target 90%
| method | radius | joint cov | cov high/low/close | worst year | Covid | 2022 | VIX>25 | earnings next day | w_proj | log-vol |
|---|---|---|---|---|---|---|---|---|---|---|
| **ewma** (scaled shape) | **aci** | **90.0** | 97.6/97.5/97.7 | 89.2 (2012) | **91.2** | 91.0 | 89.1 | 85.3 | 0.0897 | **-3.249** |
| ewma | plain | 89.9 | 97.6/97.5/97.6 | 88.7 (2012) | 89.4 | 91.4 | 88.5 | 85.3 | 0.0886 | -3.255 |
| garch | aci | 90.0 | 97.6/97.5/97.6 | 88.3 (2018) | 88.2 | 90.2 | 87.7 | 85.3 | 0.0870 | -3.270 |
| static | plain | 88.6 | 95.6/95.5/95.2 | 78.9 (2020) | 60.4 | 83.7 | 75.7 | 83.5 | (outliers) | -3.242 |
| atr_cal (box) | aci | 90.1 | 96.6/95.6/91.5 | 89.4 (2018) | 87.7 | 90.2 | 88.8 | 84.2 | **0.0629** | -2.892 |
| uni_ewma (Bonferroni box) | aci | 94.0 | 96.7/96.7/96.7 | 93.6 (2014) | 94.6 | 94.4 | 93.6 | 94.1 | 0.0778 | -2.731 |

At 95%:
- ewma+ACI: 95.0%, Covid 95.2;
- atr_cal: 95.0%, w_proj 0.0795 vs 0.1153.

Next-bar sets are on target in every regime with ACI; one-session feedback lets ACI react. The ellipsoid is far smaller in volume (-3.25 vs -2.89), but H/L/C are strongly correlated, so its axis projections are wider than a box's. Earnings next days remain about 5 pp under for every method.

### A.4 Touch probabilities (calibrated path event; `touch_reliability.json`)
US mvp, 2011-2026. For each test year, the curves are calibrated on year Y-1 (as the nightly job does). Each cell is predicted vs realised P(touch within h sessions) at a standardised distance u = |log(level/close)| / √V(h):

| h | u=0.5 | u=1.0 | u=1.5 | u=2.0 | u=2.5 | u=3.0 |
|---|---|---|---|---|---|---|
| 1 | 0.556 / 0.557 | 0.284 / 0.286 | 0.132 / 0.133 | 0.060 / 0.061 | 0.028 / 0.028 | 0.014 / 0.014 |
| 5 | 0.583 / 0.584 | 0.297 / 0.297 | 0.137 / 0.137 | 0.061 / 0.061 | 0.028 / 0.028 | 0.015 / 0.014 |
| 20 | 0.601 / 0.600 | 0.311 / 0.311 | 0.142 / 0.142 | 0.063 / 0.063 | 0.029 / 0.029 | 0.015 / 0.015 |

Calibration is within 0.2 pp. For comparison, a Gaussian reflection-principle formula gives P(u = 3) = 0.0027, 5× too low.

### A.5 Fan: marginal band at every horizon 1..20 (Technicals v2 T1, `scripts/conformal/fan_check.py`, `fan_check.json`)
The chart fan shows the calibrated marginal band for **every** horizon h = 1..20 sessions. Shape: V(h) from the same EWMA
variance term structure (h s2 + the earnings jump when an event falls in (t, t+h]); radius: the pooled (region x sector,
normal / event pool) rolling split-conformal quantile of y_h^2 / V(h), window 500, lag h, plain radius - the m_ewma
protocol of A.2 run at each h. Same data as A.2 (mvp US + EU / UK / JP, test years 2010-2026, about 2.5 M
origin-securities per horizon).

| h | per-h 80 / 90 / 95 | interpolated 80 / 90 / 95 | worst year (90) | Covid 2020 (90) | VIX>25 (90) | earnings pool (90) | mean half-width (90, log) |
|---|---|---|---|---|---|---|---|
| 1 | 80.0 / 90.0 / 94.9 | anchor | 88.9 (2010) | 87.7 | 87.9 | 89.7 | 0.0302 |
| 2 | 79.9 / 89.9 / 94.9 | 79.9 / 89.9 / 94.9 | 88.6 (2010) | 88.5 | 88.7 | 89.7 | 0.0431 |
| 3 | 79.9 / 89.9 / 94.9 | 79.7 / 89.8 / 94.9 | 88.4 (2018) | 87.0 | 88.8 | 89.7 | 0.0533 |
| 4 | 79.9 / 89.9 / 94.9 | 79.7 / 89.8 / 94.9 | 88.2 (2018) | 86.6 | 89.4 | 89.8 | 0.0619 |
| 5 | 79.9 / 89.9 / 94.9 | anchor | 88.2 (2018) | 86.6 | 89.8 | 89.7 | 0.0693 |
| 6 | 79.9 / 89.9 / 94.9 | 79.9 / 89.9 / 94.9 | 88.3 (2018) | 86.6 | 90.2 | 89.7 | 0.0761 |
| 7 | 79.9 / 89.9 / 94.9 | 79.9 / 89.9 / 94.9 | 88.2 (2020) | 86.4 | 90.6 | 89.7 | 0.0823 |
| 8 | 79.8 / 89.8 / 94.8 | 79.9 / 89.9 / 94.8 | 88.0 (2020) | 86.2 | 90.8 | 89.7 | 0.0882 |
| 9 | 79.8 / 89.8 / 94.8 | 79.9 / 89.8 / 94.8 | 87.7 (2020) | 86.1 | 91.0 | 89.6 | 0.0938 |
| 10 | 79.8 / 89.8 / 94.8 | anchor | 87.6 (2020) | 86.0 | 91.1 | 89.6 | 0.0989 |
| 11 | 79.8 / 89.8 / 94.8 | 79.8 / 89.8 / 94.8 | 87.7 (2020) | 86.4 | 91.3 | 89.6 | 0.1039 |
| 12 | 79.8 / 89.8 / 94.8 | 79.8 / 89.8 / 94.8 | 87.6 (2020) | 86.2 | 91.5 | 89.6 | 0.1087 |
| 13 | 79.8 / 89.7 / 94.7 | 79.7 / 89.8 / 94.7 | 87.5 (2017) | 86.1 | 91.7 | 89.5 | 0.1131 |
| 14 | 79.7 / 89.7 / 94.7 | 79.7 / 89.7 / 94.7 | 87.2 (2017) | 86.2 | 91.7 | 89.5 | 0.1175 |
| 15 | 79.7 / 89.7 / 94.7 | 79.6 / 89.7 / 94.7 | 86.9 (2017) | 86.2 | 91.7 | 89.5 | 0.1217 |
| 16 | 79.7 / 89.7 / 94.7 | 79.7 / 89.7 / 94.7 | 86.7 (2017) | 86.2 | 91.9 | 89.5 | 0.1256 |
| 17 | 79.6 / 89.7 / 94.6 | 79.6 / 89.7 / 94.7 | 86.5 (2017) | 86.5 | 92.0 | 89.5 | 0.1295 |
| 18 | 79.6 / 89.6 / 94.6 | 79.6 / 89.7 / 94.7 | 86.3 (2017) | 86.9 | 92.1 | 89.5 | 0.1333 |
| 19 | 79.6 / 89.6 / 94.6 | 79.6 / 89.6 / 94.7 | 86.3 (2017) | 87.3 | 92.2 | 89.4 | 0.1372 |
| 20 | 79.6 / 89.6 / 94.6 | anchor | 86.1 (2017) | 87.8 | 92.3 | 89.4 | 0.1409 |

**Reading.**
- Per-horizon calibration is on target at every h: 79.6-80.0 / 89.6-90.0 / 94.6-94.9%, the same profile as the 1/5/10/20
  anchors of A.2 (which this check reproduces exactly). Regime behaviour is that of A.2: the Covid window is 2-4 pp under
  at 90%, VIX>25 slightly over at long horizons; the earnings pool stays within 0.6 pp.
- **Choice: per-h pooled calibration, no interpolation.** Each pool holds >= 30 securities x 500 origins (>= 15,000
  scores; min_n 200), so the per-h calibration is never thin. Interpolating the radius linearly between the calibrated
  1/5/10/20 radii is equally on target (within 0.1 pp), so it is kept only as the serving fallback for envelope rows
  written before the per-h fan existed (`fan_info.source = interpolated`).
- Widths grow smoothly (about sqrt(h)); the UI draws a non-narrowing, nested envelope (80 inside 90 inside 95), which can
  only widen a band.
- Production: `conformal_envelopes` writes fan_v / fan_qm_80/90/95 (h = 1..20) for the emitted origins; nightly rolling
  coverage stays on the 1/5/10/20 anchors (`FAN_AUDIT = True` adds m2..m19 rows).

## B. Peer-group ellipsoids (walk-forward, 707,115 group-days)

| filter | radius | joint cov 90% | Covid | 2022 | VIX>25 | worst year | w_proj (90%) | log-vol | joint cov 95% | Covid (95%) | w_proj (95%) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **gcn_gru** | **aci** | **90.4** | **85.3** | 90.9 | 90.1 | 88.9 (2018) | 0.3305 | -2.442 | **95.2** | 92.5 | 0.3963 |
| gcn_gru | plain | 90.0 | 79.4 | 90.8 | 88.5 | 85.9 (2020) | **0.3264** | -2.449 | 95.0 | 89.3 | **0.3904** |
| garch_dcc | aci | 90.4 | 86.0 | 90.1 | 90.0 | 88.9 (2018) | 0.3469 | -2.464 | 95.2 | 93.3 | 0.4186 |
| garch_dcc | plain | 90.1 | 82.1 | 89.8 | 88.6 | 86.5 (2020) | 0.3426 | -2.471 | 95.0 | 91.4 | 0.4126 |
| ewma_lr (diag + rank 4) | aci | 90.3 | 90.2 | 90.7 | 91.0 | 89.1 (2023) | 0.3633 | -2.443 | 95.1 | 95.3 | 0.4423 |
| ewma (dense) | aci | 90.3 | 90.5 | 90.8 | 91.3 | 89.0 (2023) | 0.3678 | -2.452 | 95.1 | 95.4 | 0.4504 |
| ewma | plain | 90.1 | 88.9 | 90.9 | 90.8 | 87.7 (2023) | 0.3640 | -2.458 | 95.0 | 94.3 | 0.4449 |
| static_lw (paper's STATIC-CGIF) | plain / aci | 89.2 / 90.6 | 65.2 / 81.4 | 88.9 / 90.2 | 81.8 / 87.1 | 80.8 / 87.8 | 0.3590 / 0.3672 | -2.396 / -2.381 | 94.5 / 95.3 | 81.1 / 90.1 | 0.4329 / 0.4458 |
| factor (PCA rank 4) | plain / aci | 89.2 / 90.6 | 65.1 / 81.3 | 88.9 / 90.3 | 81.7 / 87.1 | 80.6 / 87.7 | 0.3728 / 0.3813 | -2.375 / -2.360 | 94.5 / 95.3 | 80.8 / 90.0 | 0.4491 / 0.4624 |
| uni (per-member CP at α/N, Bonferroni box) | plain / aci | 95.5 / 93.7 | 96.1 / 95.7 | 95.8 / 93.8 | 96.2 / 94.7 | 93.8 / 92.0 | 0.3903 / 0.3409 † | -1.476 / -1.545 | 99.7 / 97.7 | 100 / 99.8 | 0.4161 / 0.3968 † |

† Over finite boxes only. The α/N quantile with N ≈ 40 and a 500-score window is unbounded on 20.8% (plain) / 9.4% (ACI) of group-days at 90%, and on 92% / 50% at 95%.

Per group kind (90%, plain), w_proj:

| group kind | gcn_gru | garch_dcc | ewma | static |
|---|---|---|---|---|
| industry | 0.363 | 0.380 | 0.404 | 0.393 |
| supply chain | 0.281 | 0.295 | 0.314 | 0.316 |

Per-member coverage of the projections is 99.9%: projections of an N ≈ 40 joint set are very conservative as marginal bands, as the paper notes for growing N.

**Reading.** The graph-native learned filter is the sharpest at-target ellipsoid, as in the paper's moderate-N cells:
- -10.1% w_proj vs EWMA;
- -9.1% vs Ledoit-Wolf static;
- -12.4% vs the PCA factor;
- -4.7% vs GARCH-DCC.

Its log-volume is within 0.01-0.02 nats/coordinate of EWMA/GARCH, slightly larger. With ACI it meets every regime criterion; the worst is Covid at 85.3%, which is -4.7 pp and so borderline. Plain GCN-GRU fails Covid at 79.4%. EWMA+ACI is the most regime-robust (90.5% in Covid) at +10% width.

**Production for B:** gcn_gru + ACI. The weekly fit trains the shared model on the block ending 520 sessions before the as-of date; ewma_lr is the fallback.

## Economic value (Backtest Lab engine, `app.backtest._engine`; costs on; US mvp 2011 → 2026-10)

Costs: 1 bp commission, ADV-bucket half spread, square-root impact on $10M capital, borrow 50 / 300 bp. Entry is at the next close. The bands use the walk-forward EWMA envelope with the plain radius. DSR/PBO come from the engine (`deflated_sharpe`, `pbo_cscv`, 10 blocks).

**(i) Breakout on exit vs naive N-day-high breakout** (long only, max 50 positions; PBO over 12 configurations = 0.54)

| config | Sharpe | CAGR | max DD | trades |
|---|---|---|---|---|
| naive 55-day high, hold 20 | 0.92 | 16.4% | -27.4% | 9,311 |
| band (marginal 90%, h = 5), hold 20 | 0.90 | 15.4% | -31.4% | 9,243 |
| naive 20-day high, hold 10 / 20 | 0.89 / 0.88 | 15.5 / 16.6% | -22.1 / -28.0% | 18,350 / 9,591 |
| band (marginal 90%, h = 20), hold 20 | 0.75 | 10.9% | -21.2% | 7,737 |
| band (joint projection, h = 5), hold 20 | 0.62 | 5.6% | -15.7% | 3,106 |

Paired differences (conformal minus naive, same holding period):

| conformal | naive | ann. difference | t |
|---|---|---|---|
| band h5 | 20-day high | -1.4% | -0.66 |
| band h5 | 55-day high | -1.0% | -0.43 |
| band h20 | 20-day high | -5.6% | -2.32 |
| joint h5 | 20-day high | -11.3% | -3.23 |

Absolute Sharpes are mostly market beta: long-only, 2011-2026. DSR is about 0.99 for every configuration because the trial Sharpe variance is small, so it does not discriminate. **Result: FAIL.** Exiting the calibrated band is not a better breakout trigger than a naive N-day high. The joint (simultaneous) band fires too rarely.

**(ii) Band stops vs ATR stops on 12-1 momentum** (top 25 monthly, hold 21; PBO = 0.0)

| stop | Sharpe | CAGR | max DD | stops hit |
|---|---|---|---|---|
| none | 1.09 | 29.4% | -28.6% | 0 |
| band 95% (20-session lower edge) | 1.05 | 27.9% | -31.7% | 142 |
| band 90% | 1.03 | 26.8% | -31.6% | 271 |
| band 80% | 0.95 | 23.5% | -31.5% | 564 |
| 2 × ATR14 | 0.89 | 19.7% | -27.2% | 1,458 |
| 3 × ATR14 | 0.88 | 20.9% | -29.5% | 908 |

Paired differences:

| comparison | ann. difference | t | PSR |
|---|---|---|---|
| band 95% minus 3×ATR | +6.2% | +3.56 | 1.00 |
| band 90% minus 2×ATR | +6.7% | +2.69 | 1.00 |
| band 90% minus no stop | -2.2% | -2.64 | n/a |

**Result: mixed.** Calibrated band stops are significantly better than ATR stops: they trigger less often on noise. But for this momentum strategy any stop hurts, and no stop is best.

**(iii) Peer-group break-away mean reversion** (GCN-GRU walk-forward signals: the group score is above its 90% radius and a member has |z_cond| ≥ 2.5 / 3; fade the move; long-short; PBO = 0.47)

| config | Sharpe | CAGR | trades |
|---|---|---|---|
| peer break-away z ≥ 2.5, hold 5 | -0.09 | 0.9% | 9,697 |
| peer break-away z ≥ 3, hold 5 | -0.15 | 0.5% | 8,018 |
| naive fade of an own-z ≥ 2.5 move, hold 5 | -0.08 | 0.3% | 25,810 |
| naive fade z ≥ 3, hold 5 | -0.31 | -1.9% | 19,885 |

Paired difference (break-away minus naive): +0.3 to +2.1%/yr, t ≤ 1.17, not significant. **Result: FAIL (null).** Conditioning on the group does remove the worst naive fades, but there is no tradable mean reversion after costs.

## Recommended production configuration (applied in P2)

- **A (`conformal_envelopes`, nightly, full universe):**
  - EWMA shape (λ 0.94) with the earnings jump term, earnings days excluded from the variance, and separate event pools;
  - pooled region × sector calibration over a 500-origin window, re-sorted every 5 sessions;
  - **plain** radius for the joint (b*) and marginal (qm_*) bands;
  - **ACI** radius for the next-bar bands;
  - touch-probability curves stored per pool.
  - The learned GRU head is **not** deployed: +1-2%, which is immaterial.
  - Not shown in the main UI. P3 candidates are the qm_* marginal bands, explicitly labelled "calibrated band", and the touch probabilities.
- **B (`conformal_peer_groups` nightly + `conformal_peer_groups_fit` weekly):**
  - GCN-GRU (d = 32, r = 4, 20 epochs, shared across groups) with the **ACI** radius;
  - 500-session calibration window;
  - fallback ewma_lr;
  - group definitions rebuilt every 90 days.
  - UI (after P3): anomaly percentile, top contributor / break-away member, pair / spread bands. Labelled model-based; no trading-signal framing.
- **Coverage audit (`conformal_coverage`):**
  - nightly rolling audit plus these walk-forward rows;
  - Data Health flags overall < -1.5 pp or segments (n ≥ 2000) < -5 pp.

## Deviations from the paper and known limitations

- **GCN-GRU:**
  - inputs are standardised per node by the pretrain standard deviation, with features [y, |y|];
  - one model is shared across all groups, padded with masks;
  - 20 epochs instead of 60; the loss had plateaued by about epoch 15-20 in every block.
- **AgACI** aggregates the experts' levels (exponential weights on the pinball loss) rather than their bounds. It was used only in P1 tests; production uses ACI.
- **Data choices:**
  - ACI uses delayed feedback (lag 20) for the joint envelope and pooled feedback (the share of the pool's securities that missed).
  - Earnings dates in the past are taken as if pre-announced.
  - Sectors are the current classification.
  - Group membership and the mvp universe are current (survivorship bias).
  - Supply-chain edges exist only from 2023 (PIT), so earlier group graphs are purely statistical.
- **Coverage scope:** coverage is marginal, and per-regime coverage is reported, not guaranteed. The 20-session joint set cannot meet the regime criterion in a sudden crash (Feb-Mar 2020) with any method tested.
- **w_proj vs log-volume:**
  - w_proj (the axis bounding box) structurally favours boxes when the coordinates are strongly correlated (overlapping horizons; H/L/C).
  - The ellipsoid is the minimum-volume set and wins on log-volume.
  - The gate was evaluated on w_proj as specified, and both metrics are reported.
