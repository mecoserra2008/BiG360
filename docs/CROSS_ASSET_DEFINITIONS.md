# Cross-asset definitions (roadmap v2 item #5a)

Code: `backend/app/ingestion/futures_curve.py` (term structures) and `backend/app/analytics/cross_asset.py` (signals,
regime). Tables: `futures_curve`, `futures_contracts`, `cross_asset_signals`, `cross_asset_regime`, `cross_asset_corr`
(contracts in `contracts/schemas/`). Jobs: `futures_curve` (nightly), `futures_curve_backfill` (manual),
`cross_asset_signals` (nightly). State: `data/state/roadmap_v2.json` key `item5a`. Logs: `data/logs/roadmap_v2/`.

**Source policy.** Futures curves, futures front months, cash indices, ETFs and vol indices are **market data from
Yahoo** (`source = yahoo`). Macro, FX and rates inputs come from **official sources**:
- `fx_rates`: ECB > FRED H.10 > central bank; forward-filled rows are excluded.
- `cb_policy_rates`: macro W3.
- FRED: H.15 CMT yields, TIPS real yields, ICE BofA OAS and the Fed broad dollar index.
- `macro_cell_history`: CPI y/y.

Everything labelled **MODEL-BASED** below is our own transformation, not a published statistic.

## 1. Futures term structures (`futures_curve`)

- **Yahoo symbol format** (probed 2026-10-19): `{ROOT}{month code}{yy}.{exchange}`.
  - Month codes: F G H J K M N Q U V X Z.
  - Exchanges: `NYM` (NYMEX), `CMX` (COMEX), `CBT` (CBOT), `CME`, `NYB` (ICE US softs).
  - Examples: `CLZ26.NYM`, `GCZ26.CMX`, `ZNZ26.CBT`, `ESZ26.CME`, `KCZ26.NYB`, `6EZ26.CME`, `BTCZ26.CME`.
  - Formats that did not work: `CLZ26` (no suffix) and `CLZ2026.NYM`.
- **Coverage.** There are 43 roots: every continuous `=F` future already priced, minus the micro and mini duplicates
  (MGC, SIL, QM) and lumber (Yahoo serves no lumber contract months). For each root the job tracks the next *n*
  contracts of the exchange listing cycle, plus `serial` nearby calendar months: 12 for crude, products and NG, about
  4 to 8 for metals, ags and softs, and 2 to 6 for rates, equity indices, FX and crypto (`ROOTS`).
- **History.** Yahoo serves **only contracts that are still trading**; expired contracts return nothing. The backfill
  (`period=max`) therefore holds each live contract's own history:
  - CL deferred months from 2017, NG from 2013 and BZ from 2018;
  - ES quarterlies from 2021 and GC from 2020;
  - ags from 2022 to 2025;
  - Treasuries and livestock only a few months.

  On older dates the stored curve has deferred contracts only. The front of the curve accumulates from the first
  nightly run onwards.
- **Gaps.** A listed contract that Yahoo does not serve is recorded in `futures_contracts` with status `missing`, and
  is never interpolated. On 2026-10-16, 7 of 257 were missing: BTC G27, ETH F27, NQ U27, RTY M27, and TTF G27 / H27 /
  J27. `unchecked` means the request stayed throttled; `expired` means the contract is past its approximate last
  trading day.
- **Bar cleaning.** Yahoo labels the Globex Sunday-evening session with the Sunday date, and that bar is partial.
  Weekend-dated bars and bars after the as-of session are dropped. The latest bar's volume can repeat the previous
  day's value; the nightly one-month overlap upserts the corrected value.
- **expiry (APPROXIMATE).** The last trading day comes from each root's exchange rule (`EXPIRY_RULES`). The NYSE
  holiday calendar stands in for CME and ICE US business days, so the date can be off by a day around
  exchange-specific holidays.

| rule | roots | approximation |
|---|---|---|
| cl | CL | 3 business days before the 25th of the month before delivery (4 if the 25th is not a business day) |
| brent | BZ | last business day of the 2nd month before the contract month |
| ng | NG | 3 business days before the 1st of the contract month |
| last_bd_prev | RB, HO, SB | last business day of the month before the contract month |
| ttf | TTF | 2 business days before the 1st of the contract month |
| metal | GC, SI, HG, PL, PA, ALI | 3rd last business day of the contract month |
| last_bd | HRC, LE, ZT, ZF | last business day of the contract month |
| grain | ZC, ZW, KE, ZS, ZL, ZM, ZO, ZR | business day before the 15th |
| kc / cc / ct / oj | KC / CC / CT / OJ | 9th / 12th / 17th / 15th last business day of the contract month |
| he | HE | 10th business day of the contract month |
| gf | GF | last Thursday of the contract month (November exceptions ignored) |
| dc | DC | business day before the Wednesday on or before the 5th of the next month (USDA Class III announcement) |
| ust_long | ZN, ZB | 7th business day before the last business day |
| equity | ES, NQ, RTY, YM | 3rd Friday |
| fx | 6E, 6J, 6B | 2 business days before the 3rd Wednesday |
| crypto | BTC, ETH | last Friday |

- **days_to_expiry** = calendar days from the trading date to `expiry`.
- `close` is Yahoo's daily close, which is usually the exchange settlement.

## 2. Per-asset signals (`cross_asset_signals`)

**Universe.** There are 73 assets:
- commodities: 30 roots, priced from the Yahoo continuous front month `IDX:<root>=F`;
- FX: 17 pairs vs USD, built from **official** fixings, so price = quote units per 1 base;
- equity indices: 18 (cash index levels; EEM is an ETF);
- US Treasuries 2/5/10/30y: a synthetic total-return index from **official** CMT yields;
- US credit: HY and IG (HYG / LQD total return, plus official OAS);
- crypto: CME BTC and ETH front months.

The registry is `build_assets()`. Windows count rows, i.e. trading days of each asset. All returns are log returns.

**Note on front-month prices.** The `=F` front-month series is unadjusted across rolls: roll gaps are included, so it
behaves like a spot-price change rather than a futures excess return. A back-adjusted series can replace it once
`futures_curve` has enough front history.

| column | definition |
|---|---|
| mom_1m / mom_3m / mom_12m | ln(P_t / P_{t-h}), h = 21 / 63 / 252 |
| mom_12_1 | ln(P_{t-21} / P_{t-252}) |
| trend_signal | **MODEL-BASED**, in [-1, 1]. Mean over h ∈ {21, 63, 252} of clip(mom_h / (σ63 · √h), −2, 2) / 2, where σ63 is the 63-day std of daily log returns. Needs at least 2 horizons. |
| rv_1m / rv_3m | std of daily log returns over 21 / 63 rows (min 15 / 45) × √252 |
| iv | vol-index close / 100. Mapping: SPX ^VIX, NDX ^VXN, DJI ^VXD, CL ^OVX, GC ^GVZ, NIFTY ^INDIAVIX, AS51 ^AXVI. UST10Y uses ^MOVE, in bp (`iv_units`). |
| iv_rv_ratio | iv / rv_1m. UST10Y: MOVE / realised bp vol of the 10y CMT yield (21 days) |
| carry | headline carry, as an annualised fraction. `carry_method` gives the definition (table below). |
| carry_curve | futures roll yield ln(F1 / F2) · 365 / (T2 − T1). F1 and F2 are the nearest two contracts with ≥ 5 days to expiry, and the front must be within 200 days. Positive = backwardation, i.e. a long position earns the roll. FX pairs quoted USD/X use the sign-flipped curve of the X future (6J for USDJPY). |
| carry_12m | front vs the contract about 365 days further out (300 to 430 days), annualised. It is seasonality-neutral (NG, HO, grains and livestock have strongly seasonal front spreads). |
| carry_fundamental | the non-curve carry of the asset class (below) |
| curve_slope | OLS slope of ln F on years to expiry, over contracts with 5 to 400 days to expiry (at least 3). Positive = contango. Computed on the raw futures curve. |
| curve_shape | **MODEL-BASED** label: contango if slope > +1 %/yr, backwardation if < −1 %/yr, otherwise flat |
| dividend_yield | equity: trailing-365-day cash distributions of the tracking ETF / ETF close (a proxy; needs 1 year of ETF history) |
| funding_rate | equity: local policy rate; FX: quote-currency policy rate (fraction) |
| value | value proxy; `value_method` gives the definition (table below) |
| real_rate_diff | FX: (policy − CPI y/y)_base − (policy − CPI y/y)_quote, in percentage points (official inputs) |
| z_mom_12m, z_carry, z_value, z_rv, z_iv_rv, z_curve_slope | time-series z-score: (x − rolling mean) / rolling std over 756 rows (min 126) |
| cot_* | CFTC positioning (official), filled by the join described below (section 2b) |

**Carry by asset class (`carry_method`):**

| method | assets | definition |
|---|---|---|
| futures_roll | commodities, crypto | carry = carry_curve |
| fx_rate_diff | FX | (policy rate base − policy rate quote) / 100, official `cb_policy_rates`. No NZD (RBNZ is not in W3), so NZDUSD carry is null. |
| bond_carry_rolldown | UST 2/5/10/30y | (y_T − y_3m)/100 + D_mod(T, y_T) · (y_T − y_{T−1})/100: yield over 3m funding plus the 1-year roll-down on the official CMT curve (linear interpolation of 1/2/3/5/7/10/20/30y). D_mod = (1/y)(1 − (1 + y/2)^(−2T)), the modified duration of a par bond. |
| equity_div_funding | equity indices | dividend_yield − local policy rate / 100. HKD has no W3 bank, so HSI carry is null. carry_curve (ES/NQ/RTY/YM roll yield ≈ dividends − financing) is stored alongside. |
| credit_oas | US_HY, US_IG | OAS / 100: the spread over duration-matched Treasuries. Expected default losses are **not** deducted. |

**Value by asset class (`value_method`):**

| method | assets | definition |
|---|---|---|
| reversal_5y | commodities, equity indices | ln(mean P over rows t−1386 … t−1134) − ln(P_t): the price 4.5 to 5.5 years ago vs now (Asness, Moskowitz and Pedersen) |
| fx_real_5y | FX | −(Δ5y ln S + 5y CPI inflation_base − 5y CPI inflation_quote), i.e. minus the 5-year real appreciation of the base currency. The 5y inflation is Σ_{k=0..4} ln(1 + CPI y/y(t − 365k)/100), with official CPI known 46 days after the period start. It is null where CPI history is short: AUD (monthly CPI only since 2025), SEK, NOK, NZD. |
| real_yield | UST5Y / UST10Y | TIPS real yield DFII5 / DFII10 (percent) |
| oas_level | credit | OAS (percent) |

**UST synthetic price.** The daily return is y_{t−1}/100 · Δdays/365 − D_{t−1} · Δy/100, with convexity ignored. The
index is the cumulative product of (1 + return).

**Point in time.**
- Rows are dated by the asset's trading date, and `knowledge_date` = date. UST rows carry FRED's knowledge date.
- FRED inputs are taken as of their knowledge date; H.15 data is known the next day.
- CPI is taken as of the period start + 46 days.
- Policy rates are those in force on the date; dividends are taken by ex date; curve rows by trading date.

## 2b. CFTC positioning join (`cot_*`, roadmap #5b)

Done inside the `cross_asset_signals` job (`app.analytics.cross_asset.load_cot` + `join_positioning`); the job has a
soft dependency on `cftc_signals` (cross-DAG: the weekly CFTC DAG writes the lake, the nightly reads it; when the CFTC
tables are absent the columns stay null).

- **Market.** `futures_root` = `cftc_markets.root` (one market per root: active first, then curated, then the largest
  open interest). 42 of the 43 futures roots have a CFTC market; TTF has none.
- **Report and category.** Commodities (energy, metals, grains, softs, livestock): disaggregated futures-only, **managed
  money** (`disagg_fut` / `m_money`). Financial futures (equity indices, Treasuries, FX, crypto): TFF futures-only,
  **leveraged funds** (`tff_fut` / `lev_money`) - the speculative category; asset managers are structural real-money
  longs. Caveat: Treasury leveraged-fund shorts are dominated by the cash-futures basis trade, and CME BTC / ETH
  leveraged funds are structurally short (basis), so their COT index is a positioning-vs-own-history measure, not a
  directional view.
- **Point in time.** For each asset row the latest report whose **release date** (`cftc_positions.knowledge_date`,
  Friday 15:30 ET normally; observed Socrata publication time where available) is on or before the row date. The
  Friday release counts for the Friday close (end-of-day knowledge). The Tuesday as-of date is stored as
  `cot_report_date`, the release as `cot_knowledge_date`. Reports released more than 28 days before the row date are
  stale -> null (e.g. aluminium, whose CFTC market stopped reporting in 2026-06; the 2018-19 shutdown gap).
- **Columns.** `cot_index_3y` (COT index over 156 reports), `cot_z_3y` (z of net over 156 reports), `cot_net_pct_oi`
  (net / open interest), plus `cot_market_code`, `cot_report_type`, `cot_category`, `cot_inverted`.
- **Inversion.** Assets quoted inversely to their future (`curve_sign = -1`: USD/JPY vs the yen future 6J) get
  1 - COT index and negated z / % OI (`cot_inverted = true`).
- History starts with the disaggregated / TFF reports (2006-06).

## 3. Correlation regime and risk gauge (`cross_asset_regime`, `cross_asset_corr`)

**Basket (fixed).** SPX, SX5E, NKY, EEM, CL, GC, HG, UST10Y, EURUSD, USDJPY, AUDUSD, US_HY (N = 12; complete from
2007-08). The basket uses daily log returns on the union calendar:
- prices are forward-filled up to 3 days;
- only dates where ≥ 75 % of members printed are kept;
- windows need ≥ 80 % complete rows.

Closes are asynchronous across Asia, Europe and the US, which biases cross-region correlations down.

**Correlation measures:**
- `eig1_share_w`: largest eigenvalue / N of the w-day correlation matrix (w = 60, 120). It equals 1/N when there is
  no common factor and 1 when everything moves together.
- `avg_corr_w`: mean off-diagonal correlation.
- `eig1_z_120`: time-series z of eig1_share_120 (756 days, min 252).
- `cross_asset_corr`: the full matrices for the latest window and the windows ending about 21 and 252 complete rows
  earlier.

**`risk_composite` (MODEL-BASED).** This is the mean of the available component z-scores. It needs at least 4 of the
8 components. Each component z uses 756 days (min 252), is clipped to ±3, and is signed so that + = risk-on. The
components are:

| component | measure | sign |
|---|---|---|
| comp_equity_trend | S&P 500 63-day log return | + |
| comp_vix | VIX level | − |
| comp_vix_term | ln(VIX / VIX3M); inversion = stress | − |
| comp_credit_rel | 63-day change of ln(HYG / IEF), total return | + |
| comp_hy_oas | ICE BofA US HY OAS (FRED, official; history only from 2023-10) | − |
| comp_copper_gold | 63-day change of ln(copper / gold) front months | + |
| comp_audjpy | 63-day AUD/JPY log return (official fixings) | + |
| comp_usd | 63-day change of the Fed broad dollar index DTWEXBGS (official) | − |

`risk_regime`: risk_on if the composite > 0.5, risk_off if < −0.5, otherwise neutral. Sanity checks: 2008-10-15 gave
−2.8 (risk_off), 2020-03-20 gave −2.5 (risk_off) and 2017-06-15 gave +0.3 (neutral).

## 4. API and UI (roadmap #5b)

- Serving tables (`app.api.serving._Builder.cross_asset`): `xa_assets` (registry, `asset_registry()`), `xa_signals`,
  `xa_latest`, `xa_regime`, `xa_corr`, `xa_curve`, `xa_contracts`.
- API (contract v1.23.0, tag `cross-asset`): `/cross-asset/cockpit`, `/cross-asset/assets/{asset_id}`,
  `/cross-asset/curves`, `/cross-asset/curves/{root}`, `/cross-asset/correlation`, `/cross-asset/risk`.
- Model Studio source `cross_asset` (field `<asset_id>:<metric>`, PIT on `knowledge_date`).
- UI: Macro -> Cross-asset (`/macro/cross-asset`); Instrument pages show carry and curve panels when the instrument
  is linked to an asset (`asset_registry().linked_ids`).
- Curve viewer snapshots: last stored curve on or before latest - 30 days (~1m) and latest - 365 days (~1y). The ~1y
  curve has only far-dated contracts (Yahoo serves live contracts only). Curve history in the viewer is recomputed
  from `futures_curve` per root with `curve_metrics` (futures perspective, not sign-flipped).

## 5. Storage

- All tables use float32 and ZSTD (level 9 for the large ones). `futures_curve` is partitioned by root (about 3 MB);
  `cross_asset_signals` by asset class (about 15 MB, rows from 2000).
- Derived signal columns, i.e. everything except `price`, are rounded to a 12-bit mantissa (relative precision
  2.4e-4) so they compress about 30 % smaller.
- String columns are dictionary-encoded by the Parquet writer.
