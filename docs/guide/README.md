# BiG Equity 360: User guide

BiG Equity 360 is a research terminal that runs on your own machine. It gives a 360º view of listed companies (fundamentals, valuation, scores, technicals, ownership, events) and of the macro and cross-asset context around them. This guide is the reference for every page and component. For each one it covers what it is for, how to use its controls, the method behind the numbers, where the data comes from and how often it refreshes, how far the model has been validated, a worked example, and the limits to keep in mind.

The guide is written for analysts and portfolio managers who are new to the app. It is equally useful as a methodology reference. The same text is available inside the app: open **Guide** in the menu, type `HELP` in the command bar, or click the **?** icon next to any page title to jump to that page's section.

> **Note:** The screenshots were taken on 23 October 2026, with data as of 19 October 2026 (snapshot built 22 October). Your numbers will differ. The orange numbered badges in the screenshots are added by the guide. They are not part of the app. Pages that need saved user content (theses, backtests, studies, watchlists) were captured from a separate test copy with example content.

## How to read this guide

- Every page section starts with a locator line: the **route** (the address after `http://127.0.0.1:8010`), where it sits in the **menu**, the command-bar **mnemonics** that open it, and the **refresh** cadence.
- The sections then follow the same order: *Purpose*, *Screenshot* (with a table that explains each numbered callout), *Controls*, *Methodology*, *Data sources and refresh*, *Validation status*, *Correct usage and worked example*, *Limitations and pitfalls*, and *Related mnemonics and BQL fields*.
- **Labels matter.** "Model-based" means an estimate from a model, not an official statistic or a consensus figure. "Experimental" means the output failed, or has not passed, its validation gate. It is hidden unless you switch experimental outputs on. "Approximation" means a stated simplification. Probabilities are "calibrated over history", never "the chance today". Chapter 17 explains how to read them.
- Validation results are quoted from the project's validation documents. The [summary table of every model's gate status](16-methodology.md#gate-status-of-every-model) collects them in one place.

## Contents

| Chapter | What it covers |
|---|---|
| [0. Getting started](00-getting-started.md) | Install and launch (BiG360.exe and the tray), the interface tour, the command bar, keyboard shortcuts, themes, freshness indicators and labels, your first 15 minutes |
| [1. Data and coverage](01-data-and-coverage.md) | Sources by domain, universes, point-in-time rules, the serving snapshot and data-quality gate, refresh schedule (weekly by default, daily option), gap filler, coverage gaps, survivorship, licensing |
| [2A. Macro](02a-macro.md) | Macro cockpit (heatmap, cycle clock, surprise index, recession risk), release calendar with model-based expectations, nowcasts, themes, countries, series explorer, central banks and implied paths |
| [2B. Markets and cross-asset](02b-markets-cross-asset.md) | Markets overview, instruments, macro to markets (exposures, regime performance, scenario builder, episode replay), cross-asset dashboard, CFTC positioning |
| [3. Sector Explorer](03-sector-explorer.md) | Sector and subsector drill-down with sector-specific KPIs, distributions, leaders and laggards |
| [4. Screener and Ranking](04-screener.md) | Filters, factor weights and live re-ranking, the composite score and the ten factors |
| [5A. Company 360: fundamentals](05a-company-fundamentals.md) | Header, Overview, Financials, Forecast, Peers (incl. the conformal peer-group panel), Moat, Insiders, Ownership, News & Sentiment |
| [5B. Company 360: Technicals](05b-company-technicals.md) | Calibrated range fan, context chips, confluence zones, touch probabilities, the trade setup with first-passage and meta probabilities, technical score, evidence-based factors, breakouts, the experimental toggle |
| [5C. Company 360: macro, earnings, events, network and model](05c-company-macro-events-model.md) | Macro sensitivities, earnings history and model-based surprises, event risk, supply-chain network and revenue geography, the ML model |
| [6. Earnings and events calendar](06-earnings-events.md) | Earnings and corporate-events calendar and the earnings-season dashboard |
| [7. Smart Money and Insider Radar](07-smart-money-insiders.md) | 13F flows and crowding, short interest, EU / UK short register, activism, insider activity |
| [8. Global events and news](08-global-events-news.md) | GDELT world events, escalation and geopolitical risk, contagion and spillovers, event studies, and the News Terminal |
| [9. Model Studio](09-model-studio.md) | The econometrics and ML workbench: builder, methods catalogue, presets, studies and runs, results, export |
| [10. Model Lab](10-model-lab.md) | The nightly ML monitor and model registry |
| [11. Backtest Lab](11-backtest-lab.md) | Point-in-time strategy backtests: builder, signals, costs, results, robustness statistics (DSR, PBO), experimental signals |
| [12. Thesis Tracker](12-thesis-tracker.md) | Investment theses with KPIs, automatic status, performance, reviews and journal |
| [13. Watchlists and alerts](13-watchlists-alerts.md) | Watchlists, alert rules (every rule type) and fired alerts |
| [14. BQL console and mnemonics](14-bql-mnemonics.md) | The query language, field dictionary, command bar and the full mnemonic reference |
| [15. System, Data Health and QA](15-system.md) | Jobs and schedule, data freshness, the Data Health supervisor, conformal coverage audit, QA guard |
| [16. Methodology appendix](16-methodology.md) | Point-in-time data, purged CV, conformal prediction, first-passage probabilities, meta-labelling, calibration, multiple testing, and the gate status of every model |
| [17. Good practice and pitfalls](17-good-practice.md) | What not to infer, how to read probabilities, survivorship, licensing, a checklist for investment decisions |
| [18. Troubleshooting and FAQ](18-troubleshooting-faq.md) | When something does not work, logs to send to IT, frequent questions |
| [Glossary](glossary.md) | Terms and acronyms used across the app |

## Status of this edition

- Screenshots were captured between 23 and 24 October 2026 from the running app (data as of 19 October 2026). The News Terminal, the BQL console and the mnemonic command bar went live during that window and are captured from the live build.
- Pages that need saved user content (theses, backtest results, Model Studio runs, a populated watchlist, saved BQL queries) were captured from a separate test copy with example content. The "WORKER OFF" chip in those images comes from that copy.
- A few control states could not be captured (the tray menu, which is outside the browser; a hover tooltip on the range fan; some builder sections). They are marked "Screenshot pending" in the text.
- Where the guide found a label in the app that does not match what the code computes, it follows the code and says so in the relevant *Limitations and pitfalls* section.
- Most of those mismatches and the bugs the writers found were fixed on 24 Oct 2026 (API v1.39.0), after the screenshots were taken. The text describes the fixed behaviour; where a figure still shows the earlier state (for example the Screener counter "752 / 1,000", the Model Lab subtitles, the alert-rule table or the Backtest Lab "ML 3M top decile" run), the caption or the nearby text says so.

## Printing

The whole guide can be printed or saved as a PDF from the Help page (**Print / PDF**), which opens every chapter on one page in the light theme. A PDF copy may also be provided next to this file as `BiG_Equity_360_Guide.pdf`.
