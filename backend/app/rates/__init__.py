"""Rates & debt (docs/COVERAGE_V2_PLAN.md W3; owner rates-w3).

Official sovereign yield curves (long table `yield_curves`, rates as integer basis points x 100), Svensson parameters
(`yield_curve_params`), sovereign auctions (`sovereign_auctions`), listed bond reference data (`bond_reference`:
ESMA FIRDS for EU venues, OpenFIGI symbology for US TRACE bonds), issuer debt derived from filings (`company_debt`,
`debt_maturity_walls`) and the optional FINRA TRACE aggregates (`credit_market_stats`, keyed API only).

Money-market fixings (SOFR / EFFR / OBFR / TGCR / BGCR, EUR STR, SONIA, TONA, BIS policy rates), ICE BofA OAS,
Moody's Aaa / Baa, sovereign debt / deficits and TARGET balances live in the existing macro tables
(macro_series / macro_observations, contracts/macro_catalog.yaml), fetched by the macro_* jobs.

Modules: registry (curve catalogue + publication rules), common (bp x 100, HTTP, state), svensson (curve math),
sources (official parsers / loaders), curves_job (job rates_curves + backfill), auctions (job rates_auctions),
bonds (job bonds_reference), company_debt (job company_debt), trace (job credit_trace), ops_checks (gap detector
check + startup-sync rule `curve_dates`), bql (BQL fields).
"""
