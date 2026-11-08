# Supply-chain & revenue-geography map (roadmap v2 #9)

Definitions behind the Company 360 **Network & Geography** tab, the Watchlist exposure map and the
`supply-chain` API tag (api.yaml 1.27.0). Data sources are company filings and our own models. Everything
model-based is labelled as such; statistical neighbours are never shown as disclosed relationships.

## 1. Link extraction (rules only, no LLM)

**Inputs (point-in-time).** Customer and supplier relationships come from annual reports. `knowledge_date` is the filing date.

| Source | What | Module |
|---|---|---|
| XBRL `MajorCustomers` axis (SEC FSDS) | revenue by named customer / concentration % | `app.linkage.customers.xbrl_customers` |
| 10-K / 20-F / 40-F text | customer and supplier sentences, customer tables | `app.linkage.textunits` + `app.linkage.textrules` |
| GDELT co-mentions | issuer pairs in the same article (monthly) | `app.linkage.customers.gdelt_comentions` |

**Compact unit store.** `app.linkage.textunits` reads one filing at a time. Sources:
- the `sec_text` corpus;
- the latest annual report of every CIK in the landed SEC submissions. The primary document is downloaded without the HTTP cache. Foreign filers (20-F / 40-F) go first.

Only *candidate units* are kept, in `data/raw/linkage/text_units`:
- sentences with a customer / supplier / revenue keyword;
- rows of tables headed by "customer".

The HTML is never stored.

**Rules** (`RULES` in `app/linkage/textrules.py`; each rule has a prior precision `rule_conf`):

| Rule | Pattern (examples) | Prior |
|---|---|---|
| c_sales_to_pct | "Sales to Walmart Inc. accounted for 21% of net sales" | 0.95 |
| c_name_pct | "Samsung Electronics Co., Ltd. accounted for 11% of total net sales" | 0.90 |
| c_pct_from_name | "23% of our net revenue was derived from Amazon.com, Inc." | 0.90 |
| c_respectively | "Apple and Samsung accounted for 25% and 12% ..., respectively" | 0.85 |
| c_paren_pct | "McKesson (18%), Cardinal Health (15%)" | 0.80 |
| c_largest | "Our largest customer is PACCAR Inc." | 0.75 |
| c_table | customer-table row "Walmart Inc. \| 21 % \| 20 %" | 0.65 |
| c_include | "Our customers include Ford, General Motors and Stellantis" | 0.55 |
| s_purchases_pct | "Purchases from McKesson accounted for 40% of total purchases" | 0.90 |
| s_pct_from_name | "35% of our inventory was purchased from X" | 0.85 |
| s_sole | "TSMC is our sole foundry" | 0.80 |
| s_purchase_all | "We purchase substantially all of our coffee from X" | 0.80 |
| s_rely / s_made_by | "We rely on Foxconn to manufacture ...", "manufactured for us by Jabil" | 0.75 |
| s_include | "Our suppliers include BASF SE, Dow Inc." | 0.55 |

**Guards against false matches:**
- Sentences about competition ("compete", "competitors") never produce list edges.
- Combined shares are not attributed to a single name ("A and B accounted for 25%").
- Percentage rules skip names that follow a geographic preposition ("revenues generated in the Republic of Ireland").
- Names go through the generic-name filter: customer letters, markets, countries, segments and governments.
- Weak-context matches are flagged *strict*. These are list rules, table rows, "sole source" mentions, and percentage sentences about products, segments or regions. A strict match is kept only when the name resolves *exactly* to one of our issuers.

**Name to issuer matching.** Matching uses OrgLinker, with exact or fuzzy aliases. The edge confidence is `rule_conf × match_confidence`. When the name is unresolved, the confidence is 0 and the node key is `name:<normalized>`.

**Reverse edges.** A supplier named by its customer is stored as `link_type = supplier`, with `src` = the customer.
- The network treats it as the edge supplier → customer (`disclosed_supplier`).
- When both sides disclose the same pair, the edge type is `both`.

**Provenance per edge:**
- `filing_id`, `form_type` and `rule`;
- `snippet`: the matched sentence or table row, up to 200 characters;
- `knowledge_date`.

## 2. Geographic revenue beyond the US

`company_geo_revenue` combines three sources:
- **SEC FSDS** (`sec_fsds`): tagged geographic segments. This covers US filers and foreign filers that file 20-F / 40-F in XBRL.
- **IFRS geographic-axis facts** already in `fundamentals_raw` (ESEF / CMF / BIVA). This now includes revenue concepts that are specific to a single entity.
- **ESEF segment-note tables** (`source_id esef_textblock`, `app.linkage.esef_geo`). Since FY2022, the ESEF notes are block-tagged. The tables inside the segment / geographic-area text blocks are parsed with rules:
  - Row labels are mapped to countries or regions. The mapping uses Natural Earth names in de / es / fr / it / nl / pl / pt / sv, Nordic aliases and region words in 10 languages.
  - "Other Europe" is read as the region minus the countries reported separately.
  - The title, header or preceding text must read as revenue, not assets. Tables that hold several measures are split into sections.
  - The value column is the fiscal-year column.
  - The share is the value divided by the total row (when it reconciles within 3%), otherwise divided by the sum of the rows.
  - Confidence is 0.8 when the total reconciles, otherwise 0.7 (`method ixbrl_textblock_table`).

Older (pre-2022) and half-year packages are skipped. Japanese, Korean and Chinese filers are covered only where they file with the SEC. That coverage is sparse, and the UI shows a coverage badge for it.

## 3. Statistical economic neighbours (labelled statistical)

`company_neighbours` uses one security per issuer and weekly log returns over 104 weeks, requiring at least 52 common weeks. It keeps the top 10 per issuer for each measure:
- `return_corr`: Pearson correlation, at least 0.5;
- `partial_corr`: correlation of the residuals after removing the region's equal-weight market return, i.e. the partial correlation controlling for the market. Minimum 0.3.

The computation runs in blocks of 1,000 rows, so memory stays bounded. These are co-movements, not relationships.

## 4. Exposure and risk analytics (`app.supply_chain.network`, nightly)

- **Edges (`sc_edges`).** Built from company_links rows with `knowledge_date ≤ as_of` and fiscal periods in the last 3 years.
  - There is one edge per (supplier, customer) pair. The latest disclosure's share wins.
  - Edges older than 2 years are flagged `stale` and are not used for weights.
- **Direct exposure** is the latest annual breakdown known at `as_of`. Regions and rest-of-world are spread over their member countries by GDP. Without a breakdown, 100% goes to the home country (flagged).
- **Second order** = Σ over customers c with share s_c of s_c × (c's direct exposure). It sums to the disclosed customer share. Weights come from resolved customers with a share only.
- **Look-through** = (1 − Σ s_c) × direct + second order. It sums to 1. Countries and sectors are computed the same way.
- **Centrality** is computed on the supplier → customer graph:
  - PageRank (α 0.85, weight = confidence), reported × N;
  - betweenness on the resolved subgraph, sampled with k ≤ 500;
  - degree.
- **Concentration:**
  - top-customer share;
  - customer HHI = Σ s_c² over disclosed shares. This is a lower bound, because the undisclosed remainder is treated as diversified.
- **Country risk (`country_risk`, model-based).** Each component is a percentile across countries:
  - event pressure (7-day attention / negative-tone z): 0.30;
  - Hawkes P(escalation 30d): 0.20;
  - recession probability: 0.30. Preference order is NY Fed (official), then our yield-curve probit, then HMM. The euro-area value is used for EA members that have none.
  - negative macro surprises: 0.10;
  - our GPR: 0.10.

  Missing components are re-weighted, then the score is shrunk toward 50 by the covered weight: `50 + (raw − 50) × covered`. Company country risk = Σ_c w_c × risk_c, on direct and on look-through weights.

## 5. Contagion / shock propagation (model-based)

The shock is a fractional **revenue** change (−0.10 = −10%). `M[i, j]` is the transmission from node j to node i:
- **Demand channel (upstream).** Supplier j of customer c moves by `s_rev(j, c) × x_c`. Here `s_rev` is the share of j's revenue that comes from c.
- **Supply channel (downstream).** Customer c of supplier j moves by `γ × s_pur(j, c) × x_j`. Here `s_pur` is c's purchase share from j, and γ = `supply_factor` = 0.5 (partly substituted or buffered).
- Edges without a disclosed share use `default_share` = 0.05 and are flagged "assumed". Edge confidence multiplies the transmission.
- **Hops:** `x(h) = δ^(h−1) · M^h · x(0)` for h ≥ 1, with δ = `damping` = 0.5. The total is `x(0) + Σ_{h=1..H} x(h)`, with H ≤ 3. Each node is clipped to |seed|, so there is no amplification.
- **Country shock** of size m on country K: `x(0)_i = m × w_i(K)`, the direct revenue share of company i in K, followed by the same network rounds. Only companies with a geographic revenue disclosure are seeded; `include_home_country` adds home-country-only companies at 100%.
- **Statistical channel (optional).** One hop: `ρ × δ × seed`, reported separately as a co-movement proxy, not a causal effect.
- **Macro channel (optional).** The W4 factor betas (`app.macro.markets.scenarios.run_scenario`) give an equity-return estimate for each affected security, reported next to the revenue estimate. The two are not added together because their units differ.

Path shown per node: the strongest predecessor at the hop where the node is first reached.

## 6. Jobs, tables, API

**Jobs:**
- `linkage_text_units` (weekly; backfill `python -m app.linkage.textunits 6000`);
- `linkage_links` (monthly; co-mentions computed one month at a time);
- `linkage_georev` (monthly; `non_sec_only=true` rebuilds only the IFRS / ESEF partitions);
- `linkage_esef_geo` (`python -m app.linkage.esef_geo`);
- `supply_chain_neighbours` (weekly);
- `supply_chain_network` (nightly).

**Serving tables:** `sc_edges`, `sc_metrics`, `sc_exposure`, `sc_country_risk`, `sc_neighbours`.

**API:**
- `GET /companies/{id}/network?depth=1|2`
- `GET /companies/{id}/geography`
- `GET /supply-chain/country-risk`
- `POST /supply-chain/shock`
- `POST /supply-chain/portfolio-exposure`
