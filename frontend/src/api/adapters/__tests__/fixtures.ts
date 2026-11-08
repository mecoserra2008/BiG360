/**
 * Adapter fixtures. Every object is typed with the generated contract types, so a contract change that
 * breaks a fixture fails `tsc -b` before the tests even run. Shapes follow contracts/api.yaml schemas
 * (the contract carries no `example` blocks).
 */
import type {
  CClusterBuy,
  CCompanyEvents,
  CCompanyInsiders,
  CCompanyOverview,
  CCompanySentiment,
  CCountryEventAgg,
  CEventTimelinePoint,
  CFactorMeta,
  CFinancials,
  CForecast,
  CGdeltEventPage,
  CInsiderFeedPage,
  CInsiderSignalRow,
  CJobRunPage,
  CMacroRegime,
  CMarketOverview,
  CMetricTable,
  CModelExplanation,
  CModelRun,
  CModelRunDetail,
  CMoatDetail,
  CPeerComparison,
  CPriceSeries,
  CScreenerResult,
  CSectorDetail,
  CSectorSummary,
  CSecurityRef,
  CSystemStatus,
  CTaxonomy,
  CTechnicals,
  CWatchlistDetail,
  CAlert,
} from '../../contract'

export const taxonomy: CTaxonomy = {
  sectors: [
    {
      id: 'TECH',
      name: 'Technology',
      subsectors: [
        { id: 'semiconductors', name: 'Semiconductors', kpi_set_id: 'semis', groups: ['semis'] },
        { id: 'software_application', name: 'Application Software', kpi_set_id: 'software_internet', groups: ['software'] },
      ],
    },
    { id: 'FINS', name: 'Financials', subsectors: [{ id: 'banks_global', name: 'Global Banks', kpi_set_id: 'banks' }] },
  ],
}

export const factorMeta: CFactorMeta = {
  factors: [{ id: 'quality', name: 'Quality', description: null, members: { roic: 0.5 } }],
  profiles: {
    balanced: { quality: 0.18, profitability: 0.12, growth: 0.12, value: 0.15, balance_sheet: 0.08, momentum: 0.12, insider: 0.05, sentiment: 0.05, macro: 0.05, moat: 0.08 },
  },
  default_profile: 'balanced',
}

export const secMsft: CSecurityRef = {
  security_id: 'S000123',
  issuer_id: 'I000045',
  ticker: 'MSFT',
  name: 'Microsoft Corp.',
  name_local: null,
  mic: 'XNAS',
  region: 'US',
  country: 'US',
  sector_id: 'TECH',
  subsector_id: 'software_application',
  currency: 'USD',
  market_cap_usd: 3.2e12,
  logo_url: null,
}

export const secTsmc: CSecurityRef = { ...secMsft, security_id: 'S000777', issuer_id: 'I000777', ticker: '0700.HK', name: 'Tencent', mic: 'XHKG', region: 'HK', country: 'CN', subsector_id: 'semiconductors', currency: 'HKD', market_cap_usd: 5e11 }

export const marketOverview: CMarketOverview = {
  as_of: '2026-10-09',
  region: 'US',
  indices: [{ id: 'SPX', name: 'S&P 500', last: 6400, chg_1d: 0.004, chg_1m: 0.02, chg_ytd: 0.11, spark: [6300, 6400] }],
  sector_performance: [{ sector_id: 'TECH', name: 'Technology', ret_1d: 0.01, ret_1m: 0.03, ret_3m: 0.05, ret_ytd: 0.2, median_composite: 61 }],
  breadth: { pct_above_200d: 0.62, pct_above_50d: 0.55, advancers: 300, decliners: 200, new_highs_52w: 40, new_lows_52w: 5 },
  gainers: [],
  losers: [],
  regime: { date: '2026-09-30', label: 'expansion', probabilities: { expansion: 0.6, slowdown: 0.25, stagflation: 0.05, contraction: 0.05, recovery: 0.05 } },
  key_macro: [
    { series_id: 'fred:UNRATE', source_id: 'fred', title: 'Unemployment rate', country: 'US', category: 'labor', frequency: 'M', units: 'Percent', last_observation_date: '2026-09-30', last_value: 4.3, change_1p: 0.1, change_1y: 0.2, zscore_5y: 0.3, spark: [4.1, 4.2, 4.3] },
    { series_id: 'fred:BAMLC0A0CM', source_id: 'fred', title: 'US IG OAS', country: 'US', category: 'credit', frequency: 'D', units: 'Percent', last_observation_date: '2026-10-08', last_value: 0.84, change_1p: -0.02, change_1y: null, zscore_5y: -1, spark: [0.86, 0.84] },
  ],
}

export const macroRegime: CMacroRegime = {
  model_id: 'macro_hmm_v1',
  scope: 'US',
  current: { date: '2026-09-30', label: 'expansion', probabilities: { expansion: 0.6, slowdown: 0.3, contraction: 0.1 } },
  history: [
    { date: '2026-08-31', label: 'slowdown', probabilities: { slowdown: 0.55, expansion: 0.45 } },
    { date: '2026-09-30', label: 'expansion', probabilities: { expansion: 0.6, slowdown: 0.3, contraction: 0.1 } },
  ],
  inputs: [{ id: 'pmi', label: 'ISM PMI', unit: null, points: [{ date: '2026-08-31', value: 49 }, { date: '2026-09-30', value: 51.2 }] }],
}

export const screenerRow = {
  security: secMsft,
  price: 515.2,
  chg_1d: 0.012,
  chg_1m: -0.03,
  composite: 72.5,
  grade: 'B' as const,
  rank: 14,
  factor_scores: { quality: 80, balance_sheet: 66, momentum: null },
  moat_class: 'wide' as const,
  ml_rank_pct_3m: 0.81,
  insider_signal: null,
  dq_score: 0.92,
  values: { isin: 'US5949181045', ret_12m: 0.21, pe_ntm: 31.5, ev_ebit: 27.1, roic: 0.31, dividend_yield: 0.007 },
}

export const screenerResult: CScreenerResult = {
  as_of: '2026-10-09',
  total: 1,
  limit: 1000,
  offset: 0,
  weights_used: { quality: 0.25, value: 0.25, balance_sheet: 0.5 },
  rows: [screenerRow],
}

export const sectorSummaries: CSectorSummary[] = [
  { sector_id: 'TECH', name: 'Technology', n_securities: 120, market_cap_usd: 2e13, subsectors: [{ sector_id: 'TECH', subsector_id: 'semiconductors', name: 'Semiconductors', n_securities: 40 }] },
]

export const sectorDetail: CSectorDetail = {
  summary: { sector_id: 'TECH', subsector_id: 'semiconductors', name: 'Semiconductors', n_securities: 40, market_cap_usd: 8e12, ret_1m: 0.04, median_factor_scores: { composite: 58 } },
  kpi_set_ids: ['semis'],
  kpi_distributions: [{ metric_id: 'gross_margin', label: 'Gross margin', unit: 'pct', higher_is_better: true, n: 38, p10: 0.32, p25: 0.45, p50: 0.55, p75: 0.63, p90: 0.7 }],
  leaders: [screenerRow],
  laggards: [],
  factor_heatmap: [],
}

const bars = Array.from({ length: 260 }, (_, i) => {
  const d = new Date(Date.UTC(2025, 9, 1 + i))
  const c = 400 + i * 0.4
  return { t: d.toISOString().slice(0, 10), o: c - 1, h: c + 2, l: c - 2, c, v: 1e6 }
})
export const priceSeries: CPriceSeries = { security_id: 'S000123', currency: 'USD', adjusted: true, interval: 'D', bars }

export const companyOverview: CCompanyOverview = {
  security: secMsft,
  issuer: { issuer_id: 'I000045', name: 'Microsoft Corp.', country: 'US', description: 'Software.', website: 'https://microsoft.com', employees: 228000, fiscal_year_end_mmdd: '06-30', reporting_currency: 'USD', identifiers: { isin: 'US5949181045', cik: '0000789019' } },
  as_of: '2026-10-09',
  quote: { date: '2026-10-09', close: 515.2, currency: 'USD', chg_1d: 0.012, high_52w: 555, low_52w: 380, market_cap_usd: 3.83e12, enterprise_value_usd: 3.8e12, avg_dollar_volume_20d_usd: 9e9 },
  valuation: { pe_ntm: 31.5 },
  composite: { profile_id: 'balanced', score: 72.5, grade: 'B', dq_score: 95 },
  factors: [
    { factor_id: 'quality', score: 80, coverage: 1 },
    { factor_id: 'balance_sheet', score: 40, coverage: 0.8 },
  ],
  moat: { moat_class: 'wide', confidence: 0.8, moat_score: 88 },
  key_metrics: [
    { metric_id: 'roic', label: 'ROIC', factor_id: 'quality', unit: 'pct', higher_is_better: true, raw_value: 0.31, peer_percentile: 0.92, peer_median: 0.14, self_z: 0.4, metric_score: 90 },
  ],
  badges: ['wide_moat', 'stale_fundamentals', 'something_new'],
}

export const ratios: CMetricTable = {
  periods: [],
  metrics: [{ metric_id: 'roic', label: 'ROIC', unit: 'pct', values: [0.25, null, 0.28, 0.31] }],
}

const periods = [
  { period_end: '2024-06-30', fiscal_year: 2024, fiscal_period: 'FY', period_kind: 'FY' as const, knowledge_date: '2024-07-30', knowledge_date_estimated: false, is_restated: false },
  { period_end: '2025-06-30', fiscal_year: 2025, fiscal_period: 'FY', period_kind: 'FY' as const, knowledge_date: '2025-07-30', knowledge_date_estimated: false, is_restated: false },
]
export const financialsLocal: CFinancials = {
  security_id: 'S000123',
  currency: 'EUR',
  currency_basis: 'LOCAL',
  periods,
  lines: [
    { item_id: 'revenue', label: 'Revenue', statement: 'IS', unit: 'currency', indent: 0, values: [100e6, 120e6] },
    { item_id: 'eps_diluted', label: 'Diluted EPS', statement: 'IS', unit: 'per_share', indent: 1, values: [1.5, 1.8] },
    { item_id: 'total_assets', label: 'Total assets', statement: 'BS', unit: 'currency', indent: 0, values: [500e6, 520e6] },
    { item_id: 'cfo', label: 'Cash from operations', statement: 'CF', unit: 'currency', indent: 0, values: [30e6, null] },
    { item_id: 'note_x', label: 'Note', statement: 'NOTE', unit: 'pure', indent: 0, values: [1, 2] },
  ],
}
export const financialsUsd: CFinancials = {
  ...financialsLocal,
  currency: 'USD',
  currency_basis: 'USD',
  lines: financialsLocal.lines.map((l) => ({ ...l, values: l.values.map((v, i) => (typeof v === 'number' && l.unit === 'currency' ? v * (i === 0 ? 1.08 : 1.1) : v)) })),
}

const fline = (id: string, label: string, vals: number[], driver = false) => ({ item_id: id, label, is_driver: driver, values: vals })
export const forecast: CForecast = {
  as_of: '2026-10-09',
  currency: 'USD',
  years: [2024, 2025, 2026, 2027],
  actual_years: [2024, 2025],
  price: 500,
  scenarios: (['bear', 'base', 'bull'] as const).map((s, k) => ({
    scenario: s,
    probability: [0.25, 0.5, 0.25][k],
    lines: [fline('revenue', 'Revenue', [100e6, 110e6, 115e6 + k * 5e6, 120e6 + k * 10e6]), fline('revenue_growth', 'Revenue growth', [0.1, 0.1, 0.05 + k * 0.02, 0.05 + k * 0.02], true), fline('eps_diluted', 'Diluted EPS', [2, 2.2, 2.3, 2.5])],
    valuation: { wacc: 0.085, terminal_growth: 0.025, fair_value_per_share: 400 + k * 100 },
  })),
  reverse_dcf: { implied_revenue_cagr_5y: 0.12, hist_revenue_cagr_5y: 0.09 },
}

export const peers: CPeerComparison = {
  peer_group_key: 'software_application|US|large',
  level: 'subsector_region_size',
  metrics: [
    { id: 'ev_ebit', label: 'EV/EBIT', unit: 'x', higher_is_better: false, source: 'valuation' },
    { id: 'roic', label: 'ROIC', unit: 'pct', higher_is_better: true, source: 'ratios' },
  ],
  rows: [
    { security: secMsft, is_self: true, composite: 72, values: { ev_ebit: 27, roic: 0.31 } },
    { security: { ...secMsft, security_id: 'S2', ticker: 'ORCL' }, is_self: false, composite: 60, values: { ev_ebit: 22, roic: 0.18 } },
    { security: { ...secMsft, security_id: 'S3', ticker: 'CRM' }, is_self: false, composite: 55, values: { ev_ebit: 35, roic: null } },
  ],
  medians: { ev_ebit: 27, roic: 0.18 },
}

export const moat: CMoatDetail = {
  as_of: '2026-10-09',
  moat_class: 'wide',
  confidence: 0.82,
  moat_score: 88,
  components: [{ id: 'roic_spread', label: 'ROIC − WACC', weight: 0.4, score: 91, raw: 0.18 }],
  sources_of_moat: ['switching_costs', 'network_effects'],
  roic_vs_wacc: [{ fiscal_year: 2024, roic: 0.3, wacc: 0.085 }],
  history: [
    { date: '2025-09-30', moat_class: 'wide', moat_score: 80 },
    { date: '2026-09-30', moat_class: 'wide', moat_score: 88 },
  ],
}

const txn = (id: string, type: 'buy' | 'sell' | 'grant', date: string, value: number) => ({
  txn_id: id,
  insider_name: 'Jane Doe',
  role_norm: 'cfo',
  officer_title: null,
  is_director: false,
  is_officer: true,
  transaction_date: date,
  knowledge_date: date,
  txn_code: type === 'buy' ? 'P' : type === 'sell' ? 'S' : null,
  txn_type: type,
  is_open_market: type !== 'grant',
  shares: 1000,
  price: 100,
  currency: 'USD',
  value_usd: value,
  shares_owned_after: 5000,
  holding_change_pct: 0.25,
  filing_url: null,
})
export const companyInsiders: CCompanyInsiders = {
  signal: { as_of_date: '2026-10-01', signal_score: 0.7, net_buy_value_usd_90d: 250000, n_buyers_90d: 3, n_sellers_90d: 1, cluster_buy: true, c_suite_buy: true, anomaly_score: 0.83 },
  coverage: 'full',
  transactions: [txn('t1', 'buy', '2026-09-20', 100000), txn('t2', 'sell', '2026-05-01', 50000), txn('t3', 'grant', '2026-03-01', 0)],
  monthly: [],
}

export const sentiment: CCompanySentiment = {
  sentiment_score: 62.5,
  daily: [
    { date: '2025-10-02', news_count: 3, sentiment_mean: 0.1, sentiment_ewm_7d: 0.12 },
    { date: '2025-10-03', news_count: 5, sentiment_mean: 0.3, sentiment_ewm_7d: null },
  ],
  items: [{ item_id: 'n1', published_at: '2026-10-08T12:00:00Z', title: 'Headline', url: 'https://x', source_id: 'gdelt', publisher: 'Reuters', sentiment_label: 'negative', sentiment_score: null }],
  lazy_prices: [
    { filing_id: 'f1', knowledge_date: '2025-08-01', section: 'mdna', cosine_tfidf: 0.9 },
    { filing_id: 'f2', knowledge_date: '2026-08-01', section: 'mdna', cosine_tfidf: 0.95 },
  ],
}

export const gdeltEvent = {
  global_event_id: 1234,
  event_date: '2026-10-05',
  event_code: '190',
  event_root_code: '19',
  event_category: 'conflict' as const,
  quad_class: 4,
  num_articles: 99,
  avg_tone: -4.2,
  actor1_name: 'RUSSIA',
  actor2_name: 'UKRAINE',
  action_country: 'UA',
  sector_ids: ['FINS'],
  issuer_ids: ['I000045'],
}
export const companyEvents: CCompanyEvents = { daily: [{ date: '2026-10-05', mentions: 4, avg_tone: -2 }], events: [gdeltEvent] }

export const technicals: CTechnicals = {
  as_of: '2026-10-09',
  last_close: 503.6,
  atr14: 8,
  levels: [
    { level_key: 'swing_low_d_1', level_type: 'swing_pivot', timeframe: 'D', price: 480, anchor_date: '2026-08-01', side: 'support', distance_atr: 3, distance_pct: -0.05, touches: 3 },
    { level_key: 'ma200_d', level_type: 'ma_200', timeframe: 'D', price: 482, side: 'support', distance_atr: 2.7, distance_pct: -0.04, touches: 1 },
  ],
  zones: [{ zone_id: 1, side: 'support', zone_low: 478, zone_high: 484, center: 481, density: 2, strength: 0.8, rank: 1, distance_atr: 2.8, member_level_keys: ['swing_low_d_1', 'ma200_d'], entry: 482, invalidation: 474, target: 530, reward_risk: 6 }],
  level_stats: [{ level_key: 'ma_200', n_events: 900, hit_rate: 0.61, hit_rate_shrunk: 0.6, mean_reaction_atr: 1.5 }],
  trade_plan: { zone_id: 1, side: 'support', zone_low: 478, zone_high: 484, center: 481, strength: 0.8, rank: 1, distance_atr: 2.8, member_level_keys: [], entry: 482, invalidation: 474, target: 530, reward_risk: 6 },
}

export const modelExplanation: CModelExplanation = {
  model_run_id: 'mr_42',
  model_type: 'ft_transformer',
  horizon: '3M',
  date: '2026-10-09',
  score: 0.3,
  rank_pct: 0.81,
  decile: 9,
  shap_top: [
    { feature: 'mom_12_1', label: '12-1m momentum', family: 'momentum', value: 1.2, contribution: 0.01 },
    { feature: 'accruals', family: null, value: null, contribution: -0.03 },
  ],
  history: [{ date: '2026-09-01', rank_pct: 0.7 }],
}

export const insiderFeed: CInsiderFeedPage = {
  total: 2,
  limit: 500,
  offset: 0,
  items: [
    { security: secMsft, transaction: txn('f1', 'buy', '2026-09-20', 900000), signal_score: 87 },
    { security: secTsmc, transaction: txn('f2', 'sell', '2026-09-10', 2e6), signal_score: null },
  ],
}
export const insiderSignals: CInsiderSignalRow[] = [{ security: secTsmc, signal: { as_of_date: '2026-10-01', anomaly_score: 0.66, cluster_buy: false, c_suite_buy: false, xgb_prob_up: 0.58 } }]
export const insiderClusters: CClusterBuy[] = [
  { security: secMsft, n_buyers: 3, total_value_usd: 1.4e6, first_date: '2026-09-15', last_date: '2026-09-25', insiders: ['A', 'B', 'C'] },
  { security: { ...secMsft, security_id: 'S9', ticker: 'ZZZ', name: 'Zed Corp' }, n_buyers: 4, total_value_usd: 2e6, first_date: '2026-09-01', last_date: '2026-09-09', insiders: ['D', 'E'] },
]

export const eventMap: CCountryEventAgg[] = [
  { country: 'UA', events: 400, articles: 9000, avg_tone: -5.5 },
  { country: 'US', events: 200, articles: 7000, avg_tone: -1 },
]
export const eventTimeline: CEventTimelinePoint[] = [
  { date: '2026-10-02', events: 50000, articles: 1, avg_tone: -1.5 },
  { date: '2026-10-01', events: 48000, articles: 1, avg_tone: null },
]
export const eventFeed: CGdeltEventPage = { total: 2, limit: 500, offset: 0, items: [gdeltEvent, { ...gdeltEvent, global_event_id: 99, action_country: 'US', event_category: 'economy', actor1_name: null, actor2_name: null }] }

const run = (id: string, type: CModelRun['model_type'], prod: boolean): CModelRun => ({
  model_run_id: id,
  model_type: type,
  horizon: '3M',
  universe_id: 'full',
  train_start: '2010-01-01',
  train_end: '2025-12-31',
  oos_start: '2016-01-01',
  oos_end: '2026-09-30',
  ic_mean: 0.04,
  rank_ic_mean: 0.045,
  rank_ic_ir: 0.6,
  hit_rate: 0.54,
  decile_spread_ann: 0.09,
  status: 'completed',
  is_production: prod,
  created_at: '2026-10-01T03:00:00Z',
  finished_at: '2026-10-01T03:20:00Z',
  params: { n_features: 148 },
})
export const modelRuns: CModelRun[] = [run('mr_1', 'xgboost_rank', true), run('mr_2', 'ft_transformer', true), run('mr_3', 'insider_xgb', true)]
export const modelDetails: CModelRunDetail[] = [
  {
    run: modelRuns[0],
    ic_series: [{ date: '2026-08-31', value: 0.03 }],
    rank_ic_series: [{ date: '2026-08-31', value: 0.035 }],
    cum_long_short: [{ date: '2026-08-31', value: 0.4 }],
    decile_returns: [{ decile: 1, mean_excess_return: -0.02 }, { decile: 10, mean_excess_return: 0.05 }],
    fold_metrics: [{ fold: 'f1', end_date: '2020-12-31', rank_ic: 0.04 }],
    feature_importance: [{ feature: 'mom_12_1', family: 'momentum', importance: 0.12 }, { feature: 'bs', family: 'balance_sheet', importance: 0.2 }],
  },
  { run: modelRuns[1], ic_series: [], rank_ic_series: [{ date: '2026-09-30', value: 0.05 }], cum_long_short: [], decile_returns: [{ decile: 10, mean_excess_return: 0.06 }], feature_importance: [] },
]

export const systemStatus: CSystemStatus = {
  version: '0.1.0',
  serving_run_id: 'r1',
  serving_built_at: '2026-10-09T22:00:00Z',
  data_freshness: [
    { table: 'prices_daily', last_date: '2026-10-09', last_ingested_at: '2026-10-09T21:00:00Z', lag_days: 0, row_count: 1000, status: 'fresh' },
    { table: 'insider_transactions', last_date: '2026-10-01', lag_days: 8, status: 'stale' },
  ],
  last_dag_runs: [{ dag: 'nightly', dag_run_id: 'd1', status: 'partial', started_at: '2026-10-09T02:00:00Z', finished_at: '2026-10-09T03:00:00Z', jobs_total: 20, jobs_failed: 1 }],
  keys_present: { fred: true, polygon: false },
}
export const emptyStatus: CSystemStatus = { version: '0.1.0', serving_run_id: null, data_freshness: [], last_dag_runs: [], keys_present: {} }
export const jobRuns: CJobRunPage = { total: 1, limit: 100, offset: 0, items: [{ run_id: 'j1', job_id: 'ingest_prices', status: 'failed', started_at: '2026-10-09T01:00:00Z', trigger: 'schedule', duration_s: 30, rows_written: 0, error_message: 'HTTP 429' }] }

export const watchlistDetail: CWatchlistDetail = {
  watchlist: { watchlist_id: 'w1', name: 'Default', item_count: 1, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z' },
  rows: [screenerRow],
}
export const alert: CAlert = { alert_id: 'a1', security_id: 'S000123', rule_type: 'price_cross', params: { level: 480, direction: 'below' }, enabled: true, cooldown_days: 5, created_at: '2026-10-01T00:00:00Z' }

/* ───────────── contract v1.2 fixtures ───────────── */

export const overviewV12: CMarketOverview = {
  ...marketOverview,
  data_status: 'ok',
  sector_performance: [{ ...marketOverview.sector_performance[0], market_cap_usd: 4.8e13, n_securities: 263 }],
  key_macro: [{ ...marketOverview.key_macro![0], scale: 'level', units: 'Index', spark_points: [{ date: '2026-08-31', value: 1.05 }, { date: '2026-09-30', value: 1.1 }] }],
  yield_curves: [
    {
      curve_id: 'UST',
      country: 'US',
      label: 'US Treasury',
      scale: 'percent',
      points: [
        { tenor: '10Y', tenor_months: 120, yield: 5.22, yield_1m_ago: 4.9, yield_1y_ago: 4.1 },
        { tenor: '3M', tenor_months: 3, yield: 4.23, yield_1m_ago: 3.94, yield_1y_ago: 4.01 },
        { tenor: '2Y', tenor_months: 24, yield: 4.75, yield_1m_ago: 4.39, yield_1y_ago: 3.58 },
      ],
    },
  ],
}

export const forecastV12: CForecast = {
  ...forecast,
  price: 336,
  weighted_fair_value: 267,
  model: 'dcf',
  fair_value_dcf: 185,
  fair_value_multiples: 260,
  fair_value_low: 185,
  fair_value_high: 260,
  target_price_12m: 265,
  valuation_confidence: 76.3,
  tv_share: 0.9,
  flags: ['high_tv_share'],
  confidence_reasons: [],
  cost_of_capital: { wacc: 0.112, cost_of_equity: 0.113, beta: 1.2, beta_raw: 1.06, rf: 0.0522, rf_source: 'fred:DGS10', erp: 0.05, crp: 0, flags: [] },
  sensitivity_grid: { wacc: [0.06, 0.07], terminal_growth: [0.02, 0.03], fair_value: [[300, 340], [210, 240]] },
  scenarios: forecast.scenarios.map((s) => ({ ...s, lines: [...s.lines, { item_id: 'drv_ebit_margin', label: 'drv_ebit_margin', is_driver: true, values: [null, null, 0.3, 0.31] }] })),
}

export const moatV12: CMoatDetail = {
  ...moat,
  model_variant: 'financial',
  moat_sources: [{ type: 'scale', strength: 80 }],
  components: [
    { id: 'roic_wacc_spread', label: 'ROIC-WACC spread', weight: 0.27, score: 93, raw: 0.39, threshold: 60, passed: true, history: [{ date: '2024-12-31', fiscal_year: 2024, value: 0.17, score: 0.17 }] },
    { id: 'revenue_stability', label: 'Revenue stability', weight: 0.06, score: 40, threshold: 60, passed: false, supplementary: true },
  ],
  roic_vs_wacc: [{ fiscal_year: 2024, roic: 0.3, wacc: 0.085, hurdle: 0.09 }],
  market_share_series: [{ fiscal_year: 2025, share: 0.3 }, { fiscal_year: 2024, share: null }],
}

export const insidersV12: CCompanyInsiders = {
  ...companyInsiders,
  signal: { as_of_date: '2026-10-01', cluster_buy: false, c_suite_buy: false, anomaly_score: null },
  transactions: [{ ...companyInsiders.transactions[0], transaction_date: '2026-09-02', anomaly_score: 0.93, cluster_id: 'S1:20260901', features: { role_weight: 1.5, first_buy_2y: true } }],
}

export const technicalsV12: CTechnicals = {
  ...technicals,
  setup: {
    bias: 'long',
    entry_low: 327.7,
    entry_high: 336.6,
    entry: 336.6,
    invalidation: 324.3,
    target1: 344.7,
    target2: 363.6,
    reward_risk: 0.66,
    support_zone_id: 1,
    technical_score: 81.8,
    score_components: { trend: 100, missing: null },
    score_weights: { trend: 0.25 },
    trend: 'up',
    structure_state: 'bullish',
    last_break: { kind: 'bos', direction: 'up', date: '2026-07-15', price: 317.1 },
    note: 'Long setup',
  },
  indicators: { rsi14: 56.1, sma50: 322.4, sma200: 290.4, atr14: 6.7, trend: 'up', structure_state: 'bullish' },
  pivots: [
    { date: '2026-10-01', price: 325.81, kind: 'swing_low_k2', side: 'support' },
    { date: '2026-10-01', price: 325.81, kind: 'swing_low_k3', side: 'support' },
    { date: '2026-09-20', price: 345.3, kind: 'ms_hh', label: 'HH', side: 'resistance' },
  ],
  type_reliability: [
    { frontend_type: 'anchored_vwap', n_events: 548, hit_rate_shrunk: 0.5777, edge: 0.0073 },
    { frontend_type: 'gap', n_events: 56, hit_rate_shrunk: 0.6055, edge: 0.0272 },
  ],
}
