# 17. Good practice and pitfalls

BiG Equity 360 puts a large amount of data and many models on one screen. That makes it fast to form a view, and equally fast to over-read a number. This chapter collects the cautions that apply across the app. It ends with a [checklist for using the app in an investment decision](#checklist-for-an-investment-decision) and a template for [citing the app's numbers](#citing-the-apps-numbers-in-research-notes).

---

## What not to infer

- **A score is not a forecast.** The composite score, the factor scores and the sector KPI scores are percentiles against peers and against the company's own history. A grade A says "looks strong on these metrics today", not "will outperform". None of them has passed an out-of-sample return test (see the [gate table](16-methodology.md#gate-status-of-every-model)).
- **A validated model is validated for one thing.** The peer-group conformal ellipsoid passed as a *calibrated descriptive measure*; fading the stock that breaks away from its group had **no** economic value. The 52-week-high, MAX and IVOL factors have a *rank edge* in median, cross-sectional terms, but none is profitable as a stand-alone strategy after costs. A passed calibration gate does not make a trading signal.
- **The ML model is one input.** Its out-of-sample rank IC is about 0.035. That is a weak, noisy ordering of many stocks, not a reliable call on any single one.
- **"No reliable edge" means what it says.** When a zone, factor or card states it, do not treat the number next to it as informative.
- **Fair value is a model.** The headline fair value blends a DCF and peer multiples and is partly anchored to the market price. Below a valuation confidence of 40 it is indicative only.
- **Expected R is not expected profit.** The economic check of filtering setups by expected R > 0 was a null result after costs.
- **Correlation is not a relationship.** Statistical neighbours on the Network & Geo tab are co-movements, labelled statistical; only disclosed links are customer or supplier relationships.
- **An event-study "expected reaction" is a historical average** of similar labelled shocks, with wide dispersion around it.

---

## How to read probabilities and bands

Every probability and band in the app is **calibrated over history**: across many past stocks and days in the same pool (region × sector, or region × volatility regime × earnings window), the event happened about as often as stated.

| The app says | Read it as | Not as |
|---|---|---|
| 20-session P(win) 38% | "Of past setups with this geometry and pool, about 38% reached the target first within 20 sessions." | "This trade has a 38% chance of working." |
| 90% calibrated range | "Bands built this way contained the 20-session outcome 90% of the time over 2010–2026 (walk-forward 89.6%)." | "The price will be inside this band with 90% probability." |
| Chance of touch 25% | "Historically, levels this far away (in volatility units) were touched within h sessions about 25% of the time." | "There is a 25% chance of touching this level." |

**Marginal, not conditional.** Coverage and calibration are averages over time and securities. A specific stock on a specific day can be in a situation the average hides (a pending takeover, a binary trial result, a trading halt).

**Marginal, not joint.** Each horizon of the fan is calibrated on its own. The four 90% bands at 1, 5, 10 and 20 sessions contained all four outcomes at once only 73% of the time. Do not read a fan as one joint 90% statement.

**Regime failures.** Calibration holds on average, not in every regime. In the Covid crash window (origins 15 Feb – 30 Jun 2020):

- the 20-session joint conformal set covered only 77–78% at a 90% target;
- 5- and 10-session first-passage probabilities under-predicted stop-outs by 5.1–5.6 pp (which is why they are experimental);
- zone-hold probabilities were 8.7 pp too optimistic;
- breakout follow-through was under-predicted by 17–18 pp.

A sudden shock arrives after the forecast origin, and models with 10- or 20-session outcome lags cannot react in time. In a stress episode, widen your own error bars.

**Thresholds at the extremes.** Touch probabilities beyond the calibrated tail show "<1%" and near-certain ones ">99%"; touch probabilities beyond 20 sessions are extrapolated and not validated.

---

## Model-based, experimental and approximation labels

| Label | Meaning | How to use it |
|---|---|---|
| **Model-based** | Our own estimate or transformation, not a published figure or a consensus (macro expectations, earnings expectations, smart-money score, risk composite, country risk, ...). | Fine to use as an input; say "model-based" when you quote it. |
| **Model-based expectation (not consensus)** | The "expected" value of a macro release or earnings figure comes from a time-series model, not from analysts. | A "surprise" is a surprise against our model, not against the market's consensus. The market may have expected something else. |
| **Approximation** | A simplified method from official inputs (central-bank implied path from official curves, not futures or OIS probabilities). | Directionally useful; do not quote it as "the market prices X cuts" without the caveat. |
| **Experimental** (flask icon, hidden unless **Show experimental outputs** is on) | Failed its validation gate or was never validated. | Research only. Do not cite in an investment recommendation. |
| **Rank edge** | Passed a rank-IC gate but is not profitable stand-alone after costs. | A tilt or tie-breaker, not a strategy. |
| **Labelled statistical** | Derived from price co-movement. | Not a business relationship. |
| **Projection** / **Provider est.** / **Announced** (earnings dates) | Our cadence model / Yahoo date / company or exchange schedule. | Only "Announced" is confirmed. |
| **Region not validated** | A model run outside the regions of its validation (US, EU, UK, JP for the technicals). | Treat as experimental. |

---

## Survivorship

- **Universe membership history starts on 2026-10-10.** Before that date, the mvp universes (`mvp_us`, `mvp_global`) are today's names applied to the past. Any backtest, historical screen or model evaluation on them before 2026-10-10 includes only companies that survived to 2026, which flatters returns and Sharpe ratios.
- The technicals and conformal validations state this bias explicitly (current mvp membership throughout). The v3 factor evaluation includes securities that stopped trading while they traded, but listing coverage is better for current names.
- The `full` universe keeps 753 delisted US stocks with prices; they are in no universe, so universe-based backtests do not hold them.
- **Sectors are current.** A company reclassified in the past appears in its current sector throughout its history, so sector-relative history and sector-neutral labels carry a small look-ahead.
- Peer groups and supply-chain groups are the current versions; supply-chain edges exist point in time only from 2023.

What to do: prefer the `full` universe and dates after 2026-10-10 for live tracking; for history, compare results with and without the most recent winners; treat any backtest Sharpe as an upper bound.

---

## Point-in-time versus restated data

- Fundamentals in the app are point in time: each value is shown as known at its filing date, and restatements are kept as new versions. The Financials tab shows the latest version of each period; BQL with `as_of=` or `dates=` shows what was known at the time.
- **Quoting a historical ratio:** say whether it is "as first reported" or "as restated". A backtest that uses restated numbers at past dates has look-ahead.
- **Imputed knowledge dates.** Where a source has no filing date (yfinance fundamentals, some Chinese data, macro without vintages), the app assumes publication 45–120 days after period end and flags it. The real release may have been earlier or later.
- **Macro revisions.** Where ALFRED vintages exist, the series explorer shows "first release" against "latest". A GDP or payrolls figure in a chart is usually the latest revised value, not what the market saw on the day.
- **13F** positions are as of the quarter end but known only after the filing deadline (up to 45 days later). **CFTC** positions are as of Tuesday but released on Friday. **Form 4** trades count from the filing date.
- Snapshot-only fields (`pe_fwd`, `grade`, `beta_1y`, ...) have no history; BQL refuses them for past dates.

---

## Small samples

Small samples make estimates noisy and significance tests fragile. Watch for:

- **Peer groups** that widen because fewer than 8 peers exist at the subsector × region × size level; the percentile is then against a broader, less comparable group.
- **Short histories:** news sentiment from about August 2026; GDELT event history still being backfilled; 13F history from 2023 Q1 (the smart cohort needs 4 quarters of track record); short-interest and smart-money context from 2023; insider context from 2015–16. The smart-money score's IC test used only 33 non-overlapping dates.
- **Moat** needs about 7 years of history; with fewer it shows "Insufficient data".
- **Regime-conditional statistics** (regime performance, event-study cells, CFTC extreme event studies) often rest on a handful of episodes. Always read the n and the t-statistic shown next to the average.
- **Conformal audit segments** with n < 200 are marked thin; the coverage flags ignore segments below n = 2,000.
- **Your own studies.** Model Studio warns on minimum sample sizes; respect the warning.

---

## Multiple testing when screening

Screening 20,000 securities with several filters, or trying many strategy variants, guarantees that some results look excellent by chance.

- **Expect false positives.** With 100 independent tests at the 5% level, about 5 are "significant" with no effect at all.
- **The validations corrected for this** (Holm per factor, Benjamini–Hochberg across 105 factor cells, PBO and DSR across strategy configurations; see [Multiple testing](16-methodology.md#multiple-testing)). Your own screens are not corrected.
- **In the Backtest Lab,** look at DSR (adjusted for the number of trials) and PBO, not only the Sharpe ratio. PBO near 0.5 means your chosen configuration is no better than a random pick among those you tried. Count every variant you tried, including the ones you discarded.
- **Decide before you look.** Fix the universe, filters, horizon and holding rule before running; keep a hold-out period you do not touch until the end.
- **Extreme values are often data errors.** A stock at the top of a momentum or valuation screen deserves a check for ticker reuse, unit flips and missing sessions (see the Data Health gap queue, filter **Manual**).

---

## Licensing

The app was built for **internal research use by one analyst**. `docs/it/DATA_LICENSING.md` is an engineering assessment, not legal advice; BiG Legal / Compliance and the market-data team must confirm each source before the app is used more widely.

- **No external redistribution** of data is assumed to be allowed for any source. Do not paste raw data tables from the app into client material.
- **Yahoo Finance (yfinance)** supplies non-US prices, benchmarks, futures curves, some fundamentals and earnings dates. It is **not licensed for commercial use** (personal use only, no redistribution). In a bank deployment these jobs must be replaced by a licensed vendor or the bank's market-data service.
- **Polygon (Massive)** US prices and news are plan-dependent: internal multi-user display needs a business plan and exchange agreements.
- **ICE BofA OAS** series obtained through FRED are third-party copyrighted; the local archive must be deleted or licensed before bank use.
- AKShare, BaoStock, Google News RSS and mindicador.cl are marked "switch off" for bank use; exchange-website data (TDnet, HKEX, KRX, JPX, ...) is mostly under personal-use terms.
- Official sources (SEC, ECB, Eurostat, OECD, IMF, World Bank, BIS, national statistics offices, CFTC, short-sale registers), GDELT and Natural Earth are usable **with attribution**. The Caldara–Iacoviello GPR index is academic data: cite it and confirm before commercial use.
- Common Crawl news is stored as derived metadata only; publishers keep the copyright of headlines and descriptions.
- Sentiment models: confirm the FinBERT and XLM-R model licences before bank use.

In practice: quote derived figures (a ratio, a percentile, a model output) with attribution of the underlying source; do not export or forward raw price or news data.

---

## Coverage gaps by region

Coverage is deepest in the US and thinner elsewhere. Check the coverage before comparing across regions.

| Area | Situation (October 2026) |
|---|---|
| Fundamentals | 5,022 of 24,961 securities have fundamentals. US is deepest (SEC). Japan (EDINET / TDnet), the UK and LATAM are good; the EU comes from ESEF and national filing systems where available, else yfinance. Korea is thin without the OpenDART key (not configured); China, Taiwan and India are thin or absent. Screens of the `full` universe are mostly price-only outside the US, Japan and Europe. |
| Prices | All regions, but regions update on different dates (on 23 Oct 2026: US 16 Oct, EU and UK 19 Oct, JP 13 Oct). 424 Shanghai names lack the BaoStock feed; Korean full-market prices need the KRX key. |
| Technicals models | Validated on US, EU, UK and JP only; elsewhere "region not validated" or experimental. |
| Macro models | Nowcasts only for US, euro area, UK and Japan; regional regime models are not available for every region. |
| Insiders | US, UK, Germany, Netherlands, Hong Kong and Brazil. |
| Ownership | 13F covers US-listed holdings of US-reporting managers; about 40% of recent 13F rows have no mapped security. |
| Short positions | FINRA (US) and EU / UK registers; Germany and Italy are not available. |
| Consensus estimates | Not available anywhere (no free source); expectations are model-based. |

A missing value is shown as "—" or as a neutral 50 in scores. A neutral score from missing data is not the same as an average company: check each factor's coverage.

---

## ADRs versus home listings

- For companies with an ADR and a home listing (for example TM and 7203.T Toyota, PBR and PETR4.SA), **the composite score, technicals and fair value are computed on the home listing by design**. The ADR page shows a banner with **See home listing →**.
- Prices of the ADR and the home share differ by the ADR ratio and the exchange rate; returns differ by FX moves and by trading-hour mismatches.
- Valuation ratios are computed USD-on-USD (market cap = shares × close × FX of the day, fundamentals in USD), which removes the currency mismatch between an ADR's trading currency and the company's reporting currency.
- Technical levels, setups and conformal bands of the ADR and the home listing are not interchangeable: they come from different price series, sessions and calibration pools.

---

## FX conversions

- **Daily rates** are stored as USD per unit. Precedence: ECB reference rates > Fed H.10 > yfinance; gaps are forward-filled for up to 5 days (flagged as filled).
- **Balance-sheet items** convert at the period-end rate; **income and cash-flow items** at the period-average rate; **TTM in USD** is the sum of the four quarters in USD, not the local TTM times one rate. A USD revenue growth rate therefore mixes business growth with currency moves; use the reporting-currency view for operating trends.
- **Prices** are kept in the trading currency; GBp and ZAc quotes are divided to GBP and ZAR. Residual 100× unit flips on some listings are tracked in Data Health (audit F32).
- **Known FX gaps:** TWD comes only from the weekly Fed H.10 and can lag up to 7 days; BGN ends on 2026-01-05 (euro adoption).
- BQL `with(currency=...)` converts at the rate of each date; `currency=local` keeps the trading currency.

---

## Stale data

The default refresh profile is **weekly** (Saturday 08:00 Lisbon). Between refreshes the data can be up to a week old by design, and under that profile a dataset counts as fresh up to 9 days behind its expected date.

- Look at the header: **DATA AS OF** (hover for the date per region), the **Data stale** and **Scheduler off** chips, and the live-layer strip ("NOT LIVE · LAST HEARTBEAT ..." when the real-time layer is stopped).
- Regions sit on different dates. A cross-region comparison of one-day returns may compare different sessions.
- Many panels show their own as-of date (macro observations, yield curves, CFTC report and release dates, 13F quarter and filing date). Quote the panel's date, not the header's.
- A snapshot build time (System › Backend panel) is not a data date. A freshly built snapshot can contain old data.
- When in doubt, open [Data Health](15-system.md#data-health) and check the dataset's status, data as of and Lag / SLA.

---

## Checklist for an investment decision

Before a number from the app goes into an investment decision or a recommendation:

1. **Freshness.** Header data as-of for the relevant region; Data Health status of the datasets you rely on; no red alert on the System page.
2. **Coverage.** Does the company have fundamentals from an official filing (not only yfinance)? Are the factor coverages adequate? Is it an ADR whose analytics live on the home listing?
3. **Data sanity.** Any outlier checked against the source (filing, exchange) and against the Data Health gap queue (ticker reuse, unit flips, missing sessions)?
4. **Model status.** Every model output you use is **validated** for this use (see the [gate table](16-methodology.md#gate-status-of-every-model)); nothing experimental, nothing from an unvalidated region.
5. **Wording of probabilities.** Stated as "calibrated over history", with the horizon and the pool, never as "X% chance".
6. **Labels.** Model-based expectations and surprises are not consensus; approximations are named as such.
7. **Point in time.** Historical comparisons use as-first-reported data where it matters; backtests start after 2026-10-10 or are flagged as survivorship-biased.
8. **Multiple testing.** If the idea came from a screen or a backtest, how many variants were tried? DSR and PBO checked?
9. **Independent evidence.** The thesis rests on more than one model output and on fundamentals you have read in the filing.
10. **Licensing.** No raw third-party data leaves the bank; sources attributed.
11. **Record.** Snapshot id, data as-of date and the BQL query or page used are written down (see below), and a thesis is opened in the [Thesis Tracker](12-thesis-tracker.md) so the KPIs are monitored.

---

## Citing the app's numbers in research notes

A citation should let a colleague reproduce the number. Include:

- the tool and its status: "BiG Equity 360 (internal research tool)";
- the **serving snapshot id** and the **data as-of date** (System › Overview › Backend panel, and the header; per region if relevant);
- the measure, with its horizon, universe or peer group, and currency;
- the **label**: model-based, approximation, calibrated over history, and the validation status with the document section;
- the underlying data source (SEC 10-K, ECB, FRED, ...), which carries the attribution;
- where possible, the BQL query with `as_of=` that returns the number.

**Templates**

```
Source: BiG Equity 360 (internal research tool), snapshot R20261022T2056130T13, data as of 19 Oct 2026
(US prices 16 Oct 2026). Underlying data: SEC EDGAR filings; prices from the app's price lake.
```

```
"The 20-session calibrated range is 172–205 (90%; model-based, calibrated over 2010–2026 walk-forward,
89.6% realised coverage; docs/CONFORMAL_VALIDATION.md A.2). This is a historical calibration, not a
probability for this stock."
```

```
"US CPI surprise +0.2 pp vs the app's model-based expectation (not consensus)."
```

```
"Implied Fed path: about two 25 bp cuts priced over 12 months (approximation from official Treasury
curves, not futures/OIS-implied probabilities)."
```

(The values in the last three templates are illustrative; replace them with the numbers you are citing.)

**Do not** cite experimental outputs, the composite score as a forecast, or any probability as "the chance that ...". When a figure comes from Model Studio or the Backtest Lab, give the run id and state the number of variants tried.
