import type { FactorFamily, KpiDefinition, KpiProfile } from '../types'

export interface SubsectorDef {
  id: string
  name: string
  profile: KpiProfile
  /** naming flavour for synthetic companies */
  words: string[]
}
export interface SectorDef {
  id: string
  name: string
  subsectors: SubsectorDef[]
}

export const SECTORS: SectorDef[] = [
  {
    id: 'tech',
    name: 'Information Technology',
    subsectors: [
      { id: 'software', name: 'Software', profile: 'software', words: ['Software', 'Systems', 'Cloud', 'Digital'] },
      { id: 'semis', name: 'Semiconductors', profile: 'semis', words: ['Semiconductor', 'Micro', 'Photonics', 'Silicon'] },
      { id: 'hardware', name: 'Hardware & Equipment', profile: 'general', words: ['Devices', 'Electronics', 'Instruments'] },
      { id: 'it_services', name: 'IT Services', profile: 'software', words: ['Technologies', 'Data Services', 'Consulting'] },
    ],
  },
  {
    id: 'financials',
    name: 'Financials',
    subsectors: [
      { id: 'banks', name: 'Banks', profile: 'bank', words: ['Bank', 'Bancorp', 'Financial'] },
      { id: 'insurance', name: 'Insurance', profile: 'insurance', words: ['Insurance', 'Assurance', 'Re'] },
      { id: 'capital_markets', name: 'Capital Markets', profile: 'general', words: ['Capital', 'Asset Management', 'Securities'] },
      { id: 'payments', name: 'Payments & Fintech', profile: 'software', words: ['Payments', 'Pay', 'Fintech'] },
    ],
  },
  {
    id: 'health',
    name: 'Health Care',
    subsectors: [
      { id: 'pharma', name: 'Pharmaceuticals', profile: 'pharma', words: ['Pharma', 'Pharmaceuticals'] },
      { id: 'biotech', name: 'Biotechnology', profile: 'pharma', words: ['Therapeutics', 'Biosciences', 'Bio'] },
      { id: 'medtech', name: 'Medical Devices & Tools', profile: 'general', words: ['Medical', 'Diagnostics', 'Surgical'] },
      { id: 'hc_services', name: 'Health Care Services', profile: 'general', words: ['Health', 'Care Group'] },
    ],
  },
  {
    id: 'cons_disc',
    name: 'Consumer Discretionary',
    subsectors: [
      { id: 'autos', name: 'Autos & Components', profile: 'general', words: ['Motors', 'Auto Parts', 'Mobility'] },
      { id: 'retail', name: 'Retail', profile: 'retail', words: ['Stores', 'Retail', 'Commerce'] },
      { id: 'luxury', name: 'Luxury & Apparel', profile: 'retail', words: ['Brands', 'Apparel', 'Maison'] },
      { id: 'leisure', name: 'Hotels, Restaurants & Leisure', profile: 'general', words: ['Hospitality', 'Leisure', 'Resorts'] },
      { id: 'durables', name: 'Household Durables', profile: 'general', words: ['Home', 'Appliances'] },
    ],
  },
  {
    id: 'cons_staples',
    name: 'Consumer Staples',
    subsectors: [
      { id: 'food_bev', name: 'Food & Beverage', profile: 'general', words: ['Foods', 'Beverages', 'Brewing'] },
      { id: 'household', name: 'Household & Personal Products', profile: 'general', words: ['Consumer Products', 'Hygiene'] },
      { id: 'food_retail', name: 'Food & Staples Retail', profile: 'retail', words: ['Markets', 'Supermarkets', 'Wholesale'] },
      { id: 'tobacco', name: 'Tobacco', profile: 'general', words: ['Tobacco'] },
    ],
  },
  {
    id: 'industrials',
    name: 'Industrials',
    subsectors: [
      { id: 'aerospace', name: 'Aerospace & Defense', profile: 'general', words: ['Aerospace', 'Defense Systems'] },
      { id: 'machinery', name: 'Machinery', profile: 'general', words: ['Industries', 'Machinery', 'Engineering'] },
      { id: 'electrical', name: 'Electrical Equipment', profile: 'general', words: ['Electric', 'Power Systems', 'Automation'] },
      { id: 'transport', name: 'Transportation', profile: 'general', words: ['Logistics', 'Freight', 'Airports'] },
      { id: 'trading', name: 'Trading & Conglomerates', profile: 'general', words: ['Trading', 'Holdings'] },
    ],
  },
  {
    id: 'energy',
    name: 'Energy',
    subsectors: [
      { id: 'integrated', name: 'Integrated Oil & Gas', profile: 'energy', words: ['Energy', 'Petroleum'] },
      { id: 'e_p', name: 'Exploration & Production', profile: 'energy', words: ['Oil & Gas', 'Resources'] },
      { id: 'oil_services', name: 'Energy Equipment & Services', profile: 'general', words: ['Energy Services', 'Drilling'] },
    ],
  },
  {
    id: 'materials',
    name: 'Materials',
    subsectors: [
      { id: 'chemicals', name: 'Chemicals', profile: 'general', words: ['Chemicals', 'Specialty Materials'] },
      { id: 'mining', name: 'Metals & Mining', profile: 'general', words: ['Mining', 'Metals', 'Steel'] },
      { id: 'construction_mat', name: 'Construction Materials', profile: 'general', words: ['Cement', 'Building Materials'] },
    ],
  },
  {
    id: 'comms',
    name: 'Communication Services',
    subsectors: [
      { id: 'interactive', name: 'Interactive Media & Services', profile: 'software', words: ['Interactive', 'Online', 'Media Group'] },
      { id: 'telecom', name: 'Telecom Services', profile: 'utility', words: ['Telecom', 'Communications'] },
      { id: 'entertainment', name: 'Entertainment', profile: 'general', words: ['Entertainment', 'Studios', 'Games'] },
    ],
  },
  {
    id: 'utilities',
    name: 'Utilities',
    subsectors: [
      { id: 'electric_util', name: 'Electric Utilities', profile: 'utility', words: ['Power', 'Electric', 'Utilities'] },
      { id: 'renewables', name: 'Renewable Power', profile: 'utility', words: ['Renewables', 'Green Energy'] },
    ],
  },
  {
    id: 'real_estate',
    name: 'Real Estate',
    subsectors: [
      { id: 'reits', name: 'REITs', profile: 'reit', words: ['Realty', 'Properties', 'REIT'] },
      { id: 're_dev', name: 'Real Estate Development', profile: 'reit', words: ['Land', 'Estates', 'Development'] },
    ],
  },
]

export const SUBSECTOR_INDEX: Record<string, { sector: SectorDef; sub: SubsectorDef }> = Object.fromEntries(
  SECTORS.flatMap((s) => s.subsectors.map((sub) => [sub.id, { sector: s, sub }])),
)

/* ── KPI registry per profile (definition + mock distribution params) ── */

export interface KpiSpec extends KpiDefinition {
  mu: number
  sd: number
  lo: number
  hi: number
}

const k = (
  id: string,
  label: string,
  unit: KpiDefinition['unit'],
  higherIsBetter: boolean,
  mu: number,
  sd: number,
  lo: number,
  hi: number,
  description: string,
): KpiSpec => ({ id, label, unit, higherIsBetter, mu, sd, lo, hi, description })

export const KPI_SETS: Record<KpiProfile, KpiSpec[]> = {
  general: [
    k('roic', 'ROIC', 'pct', true, 0.12, 0.08, -0.15, 0.6, 'Return on invested capital (NOPAT / avg. invested capital)'),
    k('ebitMargin', 'EBIT margin', 'pct', true, 0.15, 0.08, -0.2, 0.55, 'Operating income / revenue, LTM'),
    k('revenueGrowth', 'Revenue growth', 'pct', true, 0.06, 0.08, -0.3, 0.6, 'Revenue growth, LTM y/y'),
    k('grossMargin', 'Gross margin', 'pct', true, 0.38, 0.15, 0.05, 0.9, 'Gross profit / revenue, LTM'),
    k('fcfYield', 'FCF yield', 'pct', true, 0.045, 0.025, -0.05, 0.18, 'Free cash flow / market cap, LTM'),
    k('evEbit', 'EV/EBIT', 'multiple', false, 16, 6, 4, 60, 'Enterprise value / EBIT, NTM'),
    k('netDebtEbitda', 'Net debt/EBITDA', 'multiple', false, 1.5, 1.2, -2, 6, 'Net financial debt / EBITDA, LTM'),
    k('assetTurnover', 'Asset turnover', 'ratio', true, 0.9, 0.4, 0.1, 3, 'Revenue / avg. total assets'),
  ],
  bank: [
    k('nim', 'Net interest margin', 'pct', true, 0.028, 0.008, 0.008, 0.07, 'Net interest income / avg. earning assets'),
    k('cet1', 'CET1 ratio', 'pct', true, 0.135, 0.02, 0.09, 0.22, 'Common equity tier 1 / risk-weighted assets'),
    k('rote', 'RoTE', 'pct', true, 0.13, 0.05, -0.05, 0.3, 'Return on tangible equity, LTM'),
    k('costIncome', 'Cost/income', 'pct', false, 0.55, 0.1, 0.3, 0.85, 'Operating expenses / operating income'),
    k('nplRatio', 'NPL ratio', 'pct', false, 0.018, 0.012, 0.002, 0.08, 'Non-performing loans / gross loans'),
    k('loanDeposit', 'Loan/deposit', 'pct', false, 0.85, 0.15, 0.5, 1.3, 'Net loans / customer deposits'),
    k('ptbv', 'P/TBV', 'multiple', false, 1.2, 0.5, 0.3, 3.5, 'Price / tangible book value per share'),
    k('dividendYield', 'Dividend yield', 'pct', true, 0.05, 0.02, 0, 0.12, 'Trailing dividends / price'),
  ],
  insurance: [
    k('combinedRatio', 'Combined ratio', 'pct', false, 0.94, 0.05, 0.8, 1.1, 'Loss + expense ratio (P&C)'),
    k('solvency', 'Solvency ratio', 'pct', true, 2.0, 0.4, 1.2, 3.2, 'Solvency II / RBC coverage'),
    k('roe', 'ROE', 'pct', true, 0.13, 0.05, -0.05, 0.3, 'Return on equity, LTM'),
    k('premiumGrowth', 'Premium growth', 'pct', true, 0.07, 0.05, -0.1, 0.25, 'Gross written premiums y/y'),
    k('investmentYield', 'Investment yield', 'pct', true, 0.035, 0.01, 0.01, 0.07, 'Net investment income / invested assets'),
    k('pb', 'P/B', 'multiple', false, 1.6, 0.6, 0.5, 4, 'Price / book value'),
    k('dividendYield', 'Dividend yield', 'pct', true, 0.045, 0.02, 0, 0.1, 'Trailing dividends / price'),
  ],
  software: [
    k('revenueGrowth', 'Revenue growth', 'pct', true, 0.18, 0.12, -0.1, 0.8, 'Revenue growth, LTM y/y'),
    k('ruleOf40', 'Rule of 40', 'pct', true, 0.38, 0.15, -0.2, 0.9, 'Revenue growth + FCF margin'),
    k('grossMargin', 'Gross margin', 'pct', true, 0.76, 0.07, 0.45, 0.92, 'Gross profit / revenue'),
    k('nrr', 'Net revenue retention', 'pct', true, 1.12, 0.08, 0.92, 1.45, 'Dollar-based net retention (disclosed or inferred)'),
    k('fcfMargin', 'FCF margin', 'pct', true, 0.22, 0.12, -0.2, 0.55, 'Free cash flow / revenue'),
    k('sbcPctRev', 'SBC % revenue', 'pct', false, 0.14, 0.07, 0.01, 0.4, 'Stock-based compensation / revenue'),
    k('evSales', 'EV/Sales', 'multiple', false, 9, 5, 1, 35, 'Enterprise value / NTM revenue'),
    k('rdPctRev', 'R&D % revenue', 'pct', true, 0.2, 0.06, 0.05, 0.45, 'Research & development / revenue'),
  ],
  semis: [
    k('revenueGrowth', 'Revenue growth', 'pct', true, 0.12, 0.2, -0.4, 1.2, 'Revenue growth, LTM y/y'),
    k('grossMargin', 'Gross margin', 'pct', true, 0.55, 0.12, 0.2, 0.8, 'Gross profit / revenue'),
    k('ebitMargin', 'EBIT margin', 'pct', true, 0.28, 0.12, -0.2, 0.65, 'Operating income / revenue'),
    k('roic', 'ROIC', 'pct', true, 0.2, 0.12, -0.1, 0.8, 'Return on invested capital'),
    k('rdPctRev', 'R&D % revenue', 'pct', true, 0.17, 0.06, 0.04, 0.35, 'Research & development / revenue'),
    k('inventoryDays', 'Inventory days', 'days', false, 110, 40, 30, 250, 'Inventory / COGS × 365'),
    k('capexPctRev', 'Capex % revenue', 'pct', false, 0.12, 0.08, 0.01, 0.45, 'Capital expenditure / revenue'),
    k('evEbit', 'EV/EBIT', 'multiple', false, 22, 10, 6, 80, 'Enterprise value / EBIT, NTM'),
  ],
  reit: [
    k('ffoYield', 'FFO yield', 'pct', true, 0.06, 0.015, 0.02, 0.12, 'Funds from operations / market cap'),
    k('occupancy', 'Occupancy', 'pct', true, 0.94, 0.03, 0.8, 0.995, 'Leased area / lettable area'),
    k('ltv', 'Loan-to-value', 'pct', false, 0.38, 0.08, 0.15, 0.6, 'Net debt / portfolio value'),
    k('priceToNav', 'Price/NAV', 'multiple', false, 0.9, 0.2, 0.4, 1.6, 'Share price / EPRA NTA per share'),
    k('wale', 'WALE', 'years', true, 6, 2.5, 1.5, 15, 'Weighted average lease expiry'),
    k('ssNoiGrowth', 'Same-store NOI growth', 'pct', true, 0.03, 0.025, -0.06, 0.1, 'Like-for-like net operating income growth'),
    k('netDebtEbitda', 'Net debt/EBITDA', 'multiple', false, 7, 1.5, 3, 12, 'Net debt / EBITDA'),
    k('dividendYield', 'Dividend yield', 'pct', true, 0.055, 0.015, 0.01, 0.11, 'Trailing dividends / price'),
  ],
  energy: [
    k('productionGrowth', 'Production growth', 'pct', true, 0.03, 0.05, -0.1, 0.2, 'Hydrocarbon production y/y (boe/d)'),
    k('reserveLife', 'Reserve life (1P)', 'years', true, 11, 4, 4, 25, 'Proved reserves / annual production'),
    k('breakeven', 'FCF breakeven ($/bbl)', 'number', false, 45, 10, 25, 75, 'Brent price at which FCF covers dividend'),
    k('roace', 'ROACE', 'pct', true, 0.14, 0.07, -0.05, 0.35, 'Return on average capital employed'),
    k('fcfYield', 'FCF yield', 'pct', true, 0.08, 0.04, -0.02, 0.2, 'Free cash flow / market cap'),
    k('netDebtEbitda', 'Net debt/EBITDA', 'multiple', false, 0.9, 0.6, -0.5, 3, 'Net debt / EBITDA'),
    k('evEbitda', 'EV/EBITDA', 'multiple', false, 4.5, 1.5, 2, 10, 'Enterprise value / EBITDA, NTM'),
    k('dividendYield', 'Dividend yield', 'pct', true, 0.045, 0.02, 0, 0.12, 'Trailing dividends / price'),
  ],
  pharma: [
    k('revenueGrowth', 'Revenue growth', 'pct', true, 0.06, 0.08, -0.2, 0.5, 'Revenue growth, LTM y/y'),
    k('grossMargin', 'Gross margin', 'pct', true, 0.72, 0.08, 0.4, 0.92, 'Gross profit / revenue'),
    k('ebitMargin', 'EBIT margin', 'pct', true, 0.3, 0.1, -0.3, 0.55, 'Operating income / revenue'),
    k('rdPctRev', 'R&D % revenue', 'pct', true, 0.18, 0.06, 0.05, 0.6, 'Research & development / revenue'),
    k('loeExposure', 'LOE exposure (5y)', 'pct', false, 0.2, 0.12, 0, 0.6, 'Revenue facing loss of exclusivity within 5 years'),
    k('lateStagePipeline', 'Phase III assets', 'number', true, 12, 6, 0, 40, 'Assets in phase III / filed'),
    k('peNtm', 'P/E NTM', 'multiple', false, 16, 6, 6, 60, 'Price / next-twelve-month EPS'),
    k('fcfYield', 'FCF yield', 'pct', true, 0.05, 0.025, -0.05, 0.12, 'Free cash flow / market cap'),
  ],
  retail: [
    k('sameStoreSales', 'Like-for-like sales', 'pct', true, 0.035, 0.03, -0.08, 0.15, 'Same-store sales growth'),
    k('revenueGrowth', 'Revenue growth', 'pct', true, 0.06, 0.06, -0.15, 0.35, 'Revenue growth, LTM y/y'),
    k('grossMargin', 'Gross margin', 'pct', true, 0.32, 0.1, 0.12, 0.72, 'Gross profit / revenue'),
    k('ebitMargin', 'EBIT margin', 'pct', true, 0.07, 0.04, -0.05, 0.4, 'Operating income / revenue'),
    k('inventoryTurns', 'Inventory turns', 'ratio', true, 7, 3, 1.5, 20, 'COGS / avg. inventory'),
    k('roic', 'ROIC', 'pct', true, 0.15, 0.08, -0.05, 0.5, 'Return on invested capital'),
    k('netDebtEbitda', 'Net debt/EBITDA', 'multiple', false, 1.2, 1, -1.5, 4.5, 'Net debt (incl. leases) / EBITDA'),
    k('evEbitda', 'EV/EBITDA', 'multiple', false, 10, 4, 3, 35, 'Enterprise value / EBITDA, NTM'),
  ],
  utility: [
    k('rateBaseGrowth', 'Rate-base growth', 'pct', true, 0.06, 0.025, 0, 0.14, 'Regulated asset base CAGR guidance'),
    k('allowedRoe', 'Allowed ROE', 'pct', true, 0.098, 0.008, 0.07, 0.12, 'Weighted allowed return on equity'),
    k('netDebtEbitda', 'Net debt/EBITDA', 'multiple', false, 5, 1, 2.5, 7.5, 'Net debt / EBITDA'),
    k('ffoToDebt', 'FFO/debt', 'pct', true, 0.14, 0.03, 0.07, 0.25, 'Funds from operations / total debt'),
    k('payoutRatio', 'Payout ratio', 'pct', false, 0.68, 0.1, 0.35, 0.95, 'Dividends / earnings'),
    k('renewablesShare', 'Renewables share', 'pct', true, 0.35, 0.2, 0, 0.95, 'Renewable share of generation capacity'),
    k('peNtm', 'P/E NTM', 'multiple', false, 17, 3, 8, 30, 'Price / next-twelve-month EPS'),
    k('dividendYield', 'Dividend yield', 'pct', true, 0.042, 0.01, 0.01, 0.08, 'Trailing dividends / price'),
  ],
}

export { FACTOR_FAMILIES, DEFAULT_WEIGHTS } from '@/lib/factors'

export const SUBFACTORS: Record<FactorFamily, string[]> = {
  quality: ['Piotroski F-score', 'Accruals ratio', 'Earnings stability', 'Beneish M-score'],
  profitability: ['ROIC', 'Gross profit / assets', 'EBIT margin', 'Cash conversion'],
  growth: ['Revenue CAGR 3y', 'EPS revisions 3m', 'FCF growth', 'Reinvestment rate'],
  value: ['EV/EBIT', 'FCF yield', 'P/B vs ROE', 'Reverse-DCF gap'],
  balanceSheet: ['Net debt/EBITDA', 'Altman Z', 'Interest cover', 'Maturity wall'],
  momentum: ['12-1m return', '6m return', 'Earnings drift', 'Trend strength'],
  insider: ['Net buying 90d', 'Cluster buys', 'Anomaly score', '13F accumulation'],
  sentiment: ['News sentiment 30d', 'Tone change', 'Lazy-prices similarity', 'Short interest'],
  macro: ['Regime fit', 'Rate sensitivity', 'FX exposure', 'Country risk'],
  moat: ['ROIC − WACC spread', 'Margin stability', 'Margin premium', 'Market share trend'],
}
