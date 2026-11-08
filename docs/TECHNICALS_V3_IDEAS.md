# Technicals v3: research ideas (shortlist)

**Status:** ideas for the user to choose from. Nothing is approved yet.
**Rule carried over from v2:** every probability or signal ships only after a walk-forward gate (calibration, a regime check, baselines, and leakage tests) with an honest economic check. Anything that fails is shown as experimental or hidden.

**Context:** v2 found that the heuristic technical score and zone "strength" have **no out-of-sample edge**. Calibrated *uncertainty* does work: the cone, touch probabilities and the 20-day first-passage model. So v3 leans on:
- (a) evidence-based technical factors from the academic literature, in place of heuristics;
- (b) conditioning the calibrated probabilities on richer context;
- (c) tools that turn probabilities into decisions (sizing, alerts).

## Ranked shortlist

| # | Idea | What it adds | Evidence / reference | Data we have | Effort | Risk of null |
|---|---|---|---|---|---|---|
| 1 | **Evidence-based technical factor panel** (replaces the heuristic score) | Per stock: 52-week-high proximity, 12-1 momentum, residual (idiosyncratic) momentum, 1-month reversal, MAX effect, idiosyncratic volatility. Each is a percentile within region × sector, and each shows its *own* out-of-sample IC on our data. | George & Hwang 2004 (52w high); Jegadeesh–Titman 1993; Blitz–Huij–Martens 2011 (residual momentum); Bali–Cakici–Whitelaw 2011 (MAX); Ang et al. 2006 (IVOL) | prices 15y+, factor_scores, sectors | M | low–medium: these are the best-documented technical effects |
| 2 | **Meta-labelled setups (triple barrier)** | Extends T2: the probability that a setup wins, conditioned on context: market regime, peer-group anomaly, smart-money/insider flow, sentiment, earnings proximity, squeeze state, volume confirmation. A secondary model decides *whether* to take a setup and how big. | López de Prado 2018, *Advances in Financial ML*, ch. 3 (meta-labelling, triple barrier) | all tables already exist; T2 events (3.2M origins) | M–L | medium |
| 3 | **Analog / nearest-neighbour forecasting ("similar setups")** | Find the k most similar historical windows across the universe: normalised price shape (DTW / matrix profile), volatility, trend, zone geometry. Draw their realised forward paths as faint lines inside the cone, with a conformal outcome distribution. Very intuitive for users. | Lo–Mamaysky–Wang 2000 (pattern recognition); matrix profile (Yeh et al. 2016); conformal kNN | prices; the conformal library | M | medium. The calibrated *distribution* is useful even with no edge in the mean. |
| 4 | **Position sizing & risk calculator** | From account size and risk %: shares, $ at risk, a probability-weighted P&L distribution (T2 + cone), volatility-targeted size, and a fractional-Kelly cap on expected R. Links to theses and watchlists. | standard risk management; vol targeting (Moreira–Muir 2017) | T2 outputs, cone, FX | S | none: it is a tool, not a signal |
| 5 | **Volume-confirmed breakouts & accumulation** | Breakout quality from daily data: relative volume, OBV/CMF divergence, the Amihud illiquidity change, the close position in the range. Is the probability of follow-through higher than for unconfirmed breakouts? | Lee–Swaminathan 2000 (volume & momentum); Gervais–Kaniel–Mingelgrin 2001 (high-volume return premium) | daily OHLCV | S–M | medium |
| 6 | **Price regime detector (trend vs mean-revert)** | Variance ratio, Hurst/DFA, return autocorrelation, and BOCPD change points (BOCPD is already in the event models) applied to each stock's price. It says *which* toolkit to trust now: breakout rules in trending regimes, zone fades in mean-reverting ones. Change points are marked on the chart. | Lo–MacKinlay 1988 (variance ratio); Adams–MacKay 2007 (BOCPD) | prices; the BOCPD implementation | M | medium |
| 7 | **Relative technicals vs sector / peers / index** | Relative-strength line, RS rank, and a "relative breakout" against the peer group, using the conformal pair-band so the RS move is calibrated. Answers "is this stock or its sector moving?" | Moskowitz–Grinblatt 1999 (industry momentum) | peer groups, sector indices | S–M | low–medium |
| 8 | **Event-aware levels: earnings gaps & gap fills** | The probability that a post-earnings gap fills within h days. Post-earnings drift is shown on the chart, along with an earnings-day cone (separate pool), and the same for ex-dividend and index-rebalance days. | Bernard–Thomas 1989 (PEAD); gap studies | earnings_events/reactions, corporate_actions | S–M | medium |
| 9 | **Market & breadth context on every chart** | The share of peers above their 50/200-day averages, sector RS, the cross-asset risk-on/off gauge, and VIX regime. The calibrated probabilities are re-pooled by market state. | Zweig breadth; regime pooling, as in conformal | existing tables | S | low |
| 10 | **Smart alerts from probabilities** | "Approaching a zone with an edge (touch-5d ≥ 60%)", "squeeze started (range width ≤ p10)", "setup expected R turned positive", "close outside the 95% cone". Delivered by tray toast and the watchlist. | tooling | all v2 outputs | S | none |
| 11 | **Seasonality with multiple-testing control** | Turn-of-month, day-of-week, pre-holiday and month-of-year effects per stock and sector, with Bonferroni/BH correction and an out-of-sample check. It shows "nothing significant" honestly when that is the case. | Ariel 1987; Lakonishok–Smidt 1988 | prices | S | high (most per-stock seasonality is noise; the honest display is still useful) |
| 12 | **Explainable setup narrative** (template-based, no LLM) | A plain-language "why": the drivers of the probability (top features of the meta-label model), the risks (earnings, squeeze, regime), and the historical analogs. | SHAP for the meta-model | the outputs of 2 and 3 | S (after 2/3) | n/a |

## Suggested bundles
- **Bundle A, "decision tools"** (fast and certain value): 4 sizing + 10 alerts + 9 market context. No validation risk.
- **Bundle B, "real edges"** (research): 1 evidence-based factors + 2 meta-labelling + 5 volume-confirmed breakouts. Most likely to produce a *validated* signal.
- **Bundle C, "intuition"**: 3 analog paths + 6 regime detector + 12 narrative. Turns the probabilities into something a PM can read at a glance.
- **Later:** 7 relative technicals, 8 event-aware levels, 11 seasonality.

## What we deliberately skip
- **Intraday patterns and order flow:** we have daily data only, and intraday feeds would need licensing.
- **Options-implied levels** (max pain, gamma walls, implied moves per stock): no licensed options data. Only VIX-type indices are available.
- **Black-box deep "price prediction" models:** the FT-Transformer lesson (it failed the leakage test). Any ML stays inside the meta-labelling framework with purged CV and permutation tests.

## Progress

### Bundle B #1 evidence-based factors / #5 volume-confirmed breakouts (gate: docs/TECHNICALS_V3_VALIDATION.md)
- [x] Factor math (`backend/app/techv3/factors.py`): hi52, 12-1 mom, residual momentum (36-month OLS on market + sector, BHM scaling), 1-month reversal, MAX, IVOL, industry momentum; chunked per-region pipeline (`build.py`, `panel.py`); pytest vs hand / brute-force cases + causal invariance.
- [x] Breakout events, features, first-passage labels, geometry-matched random controls (`breakouts.py`); calibrated P(follow-through) (`bo_model.py`).
- [x] Research build 2004 -> 2026 for US / EU / UK / JP (`scripts/technicals_v3/wf_build.py`, resumable, `wf_chain.ps1`).
- [x] Walk-forward evaluation: rank IC 5 / 20 / 60 d, NW t, BH / Holm, deciles, years, regimes, regions, size buckets (micro separate), shift-by-one + label permutation + positive control; walk-forward composite (selected EW, all-EW, ridge) vs the v2 heuristic score (`wf_eval.py`).
- [x] Breakout study: confirmed vs unconfirmed vs random controls, geometry-adjusted effect, purged walk-forward model, ECE / regimes / baselines / permutation (`wf_breakouts.py`).
- [x] Economic check in the Backtest Lab engine (quintile long-short / long-only, breakout events; DSR, PBO) (`econ.py`), log vs simple decile spreads (`spreads.py`); gate verdicts (`report.py` -> `data/reports/technicals_v3/gate.json`).
- [x] Tables `tech_factors`, `tech_factor_eval`, `tech_breakouts`, `tech_breakout_state`; jobs `tech_factors` / `tech_breakouts` (nightly), `tech_factor_eval` (monthly) in contracts/jobs.yaml v35.
- [x] Serving builder `technicals_v3`, API v1.36.0 GET /technicals/{id}/factors and /breakout; Studio / Backtest Lab source `tech_factors`.
- [x] UI: "Technical factors (evidence-based)" card, Breakout panel, composite headline (only if it passes: it did not).
- [x] Coordination keys b1 / b5 in data/state/technicals_v3.json.
