import type { AvailabilityReason } from '@/lib/availability'
/**
 * BiG Equity 360 — API payload types.
 *
 * These are the shapes the UI consumes. The mock layer (src/api/mock) produces them
 * deterministically; the HTTP layer (src/api/client.ts) expects the FastAPI backend to
 * return the same JSON (camelCase keys, ISO-8601 dates "YYYY-MM-DD" / full timestamps).
 * Monetary values are in the stated currency, in absolute units unless a field says *_Mn.
 * Percentages are decimals (0.123 = 12.3%) unless the field name ends in "Pct100".
 * Scores are 0–100 percentiles unless stated otherwise.
 */

/* ───────────────────────────── primitives ───────────────────────────── */

export type ISODate = string // "2026-10-09"
export type ISODateTime = string // "2026-10-09T21:15:00Z"

/** 'OTHER' collects contract regions without a UI chip (CA, APAC, OTHER). */
export type RegionCode = 'US' | 'EU' | 'UK' | 'JP' | 'KR' | 'CNHK' | 'LATAM' | 'OTHER'
export type RegionFilter = 'ALL' | RegionCode
/** ISO 3166-1 alpha-2 */
export type CountryCode = string
/** ISO 4217 */
export type CurrencyCode = string

export type FactorFamily =
  | 'quality'
  | 'profitability'
  | 'growth'
  | 'value'
  | 'balanceSheet'
  | 'momentum'
  | 'insider'
  | 'sentiment'
  | 'macro'
  | 'moat'

export type FactorScores = Record<FactorFamily, number>
export type FactorWeights = Record<FactorFamily, number>

/** insufficient = 'insufficient_data' (fewer than seven years of history): shown as such, not as No moat (guide 05a) */
export type MoatRating = 'wide' | 'narrow' | 'none' | 'insufficient'
export type DataQuality = 'high' | 'medium' | 'low'
export type MarketCapBucket = 'mega' | 'large' | 'mid' | 'small' | 'micro'

/** Sector-specific KPI set selector. */
export type KpiProfile =
  | 'general'
  | 'bank'
  | 'insurance'
  | 'software'
  | 'semis'
  | 'reit'
  | 'energy'
  | 'pharma'
  | 'retail'
  | 'utility'

export type ValueUnit =
  | 'pct' // decimal ratio shown as %
  | 'bps'
  | 'multiple' // x
  | 'currency' // absolute currency amount
  | 'number'
  | 'ratio' // plain decimal
  | 'days'
  | 'years'
  | 'score' // 0–100

/** v1.2 contract: completeness of a payload. 'pending' = pipeline has not produced it yet. */
export type DataStatus = 'ok' | 'partial' | 'pending'

export interface TimePoint {
  date: ISODate
  value: number
}

export interface PriceBar {
  time: ISODate
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface Paged<T> {
  items: T[]
  total: number
  asOf: ISODate
}

/* ───────────────────────────── universe / search ───────────────────────────── */

export interface CompanySummary {
  /** Stable security id, Yahoo-style symbol: "AAPL", "MC.PA", "7203.T", "005930.KS", "0700.HK" */
  id: string
  ticker: string
  name: string
  exchange: string
  isin: string
  country: CountryCode
  region: RegionCode
  currency: CurrencyCode
  sector: string
  subsector: string
  kpiProfile: KpiProfile
  marketCapUsd: number
  marketCapBucket: MarketCapBucket
  price: number
  change1dPct: number
  compositeScore: number
  factorScores: FactorScores
  moat: MoatRating
  dataQuality: DataQuality
}

export interface ScreenerRow extends CompanySummary {
  rank: number
  /** A-E quintile of the server composite under the weights of the request (guide 04) */
  grade?: string | null
  /** v1.44: mvp | extended (anchored to the MVP reference cross-section) */
  scoreScope?: 'mvp' | 'extended' | null
  /** v1.44: why the composite is empty */
  scoreReason?: AvailabilityReason | null
  return1m: number
  return12m: number
  peNtm: number | null
  evEbit: number | null
  fcfYield: number | null
  roic: number | null
  revenueGrowth: number | null
  netDebtEbitda: number | null
  dividendYield: number | null
  modelExpectedExcess: number
  /** Model-based macro betas (macro-w4; null when not estimated). Rates / credit: return per +100bp; USD / oil: elasticity; growth surprise: per +100 index points. */
  mxBetaRates: number | null
  mxBetaUsd: number | null
  mxBetaOil: number | null
  mxBetaCredit: number | null
  mxBetaGrowthSurprise: number | null
  /** Model-implied return (fraction) if rates fall 100bp (parallel 2y and 10y). */
  mxImpactRatesDown100: number | null
}

export interface ScreenerPayload {
  dataStatus?: DataStatus
  asOf: ISODate
  rows: ScreenerRow[]
  defaultWeights: FactorWeights
  /** coverage v3 A8: composite_label === 'validated' (every composite on the page inside a passed gate's population) */
  compositeValidated?: boolean
}

export interface SearchResult {
  id: string
  ticker: string
  name: string
  isin: string
  exchange: string
  country: CountryCode
  sector: string
  /** contract sector id (e.g. TECH) */
  sectorId?: string | null
}

/* ───────────────────────────── market & macro ───────────────────────────── */

export type RegimeName = 'Expansion' | 'Slowdown' | 'Contraction' | 'Recovery' | 'Stagflation'

/**
 * modelled = the requested scope has its own regime model; fallback = the GLOBAL model answers (shown labelled);
 * not_modelled = no model for the scope (never show a regime label).
 */
export type RegimeStatus = 'modelled' | 'fallback' | 'not_modelled'

export interface RegimeDriver {
  label: string
  /** formatted level in its true unit (never a z-score) */
  value: string
  /** model z-score of the latest level (v1.29), null when unknown */
  z: number | null
  direction: 'up' | 'down' | 'flat'
}

export interface MacroRegime {
  status: RegimeStatus
  /** scope the user asked for (GLOBAL, US, EU, KR …) */
  requestedScope: string
  /** scope of the model that produced the label (GLOBAL when falling back) */
  scope: string | null
  modelId: string | null
  /** null when not modelled or the label is unknown */
  current: RegimeName | null
  date: ISODate | null
  probability: number
  probabilities: Record<RegimeName, number>
  /** states the model actually has (e.g. 4 HMM states) — only these are shown */
  states: RegimeName[]
  history: { date: ISODate; regime: RegimeName | null; probability: number }[]
  drivers: RegimeDriver[]
  message?: string | null
  availableScopes?: string[]
}

export interface YieldCurvePoint {
  tenor: string
  years: number
  yield: number
  /** O/N policy / overnight rate, not a market yield (v1.29; O/N before) */
  isPolicyRate?: boolean
}

export interface YieldCurve {
  country: CountryCode
  label: string
  region?: string | null
  /** market tenors only (policy rate excluded) */
  current: YieldCurvePoint[]
  oneMonthAgo: YieldCurvePoint[]
  oneYearAgo: YieldCurvePoint[]
  /** policy / overnight rate shown as a marker, not as a tenor */
  policyRate: number | null
  spread2s10sBps: number
  spread5s30sBps: number
  change10y1mBps: number
  /** newest point date */
  asOf: ISODate | null
  /** oldest point date (curves mixing dates) */
  asOfMin: ISODate | null
  nMarketTenors: number
}

export interface MacroIndicator {
  id: string
  name: string
  region: RegionCode
  /** country code of the series (US, DE, CN …) */
  country?: string | null
  unit: ValueUnit
  value: number
  change: number
  changeLabel: string
  higherIsBetter: boolean | null
  /** reference date of `value` */
  asOf: ISODate
  source: string
  history: TimePoint[]
  /** y/y, q/q SAAR, m/m change … (v1.29 display transform); null = level */
  displayLabel?: string | null
  /** calendar window of `history` in days (v1.29); null = legacy last-N observations */
  sparkWindowDays?: number | null
  /** v1.29 transform applied to the display value */
  transform?: string | null
}

export interface CreditSpread {
  id: string
  name: string
  region?: string | null
  /** oas = option-adjusted; yield_spread = e.g. Moody's Baa minus 10Y (not option-adjusted) */
  spreadType: 'oas' | 'yield_spread' | 'unknown'
  segment?: string | null
  asOf: ISODate | null
  valueBps: number
  change1dBps: number
  /** NaN when the API does not provide a calendar 1M change (pre-1.29) */
  change1mBps: number
  /** empirical percentile 0–100 within `percentileWindowYears`; NaN when unknown */
  percentile: number
  percentileWindowYears: number | null
  sparkWindowDays: number | null
  history: TimePoint[]
}

export interface SectorPerformance {
  sector: string
  sectorId?: string
  region: RegionCode
  return1d: number
  return1m: number
  returnYtd: number
  /** null = unknown (treemap leaves it out) */
  marketCapUsd: number | null
  nSecurities: number | null
  /** constituents behind the 1D return (only names on the region's data date, v1.29) */
  nRet1d: number | null
  /** session of the 1D return when all regions of the row share it (v1.29) */
  asOf: ISODate | null
  asOfByRegion: Record<string, ISODate>
}

/** v1.29 explicit data-as-of of a region's equities (modal last-bar date). */
export interface RegionDataAsOf {
  region: string
  asOf: ISODate
  latest: ISODate | null
  nSecurities: number | null
  shareAtAsOf: number | null
  staleShare: number | null
}

export interface IndexQuote {
  id: string
  name: string
  region: string
  currency: string | null
  last: number
  change1d: number
  change1w: number
  change1m: number
  changeYtd: number
  asOf: ISODate | null
  spark: TimePoint[]
  /** |1D| above two standard deviations of the spark's daily moves */
  bigMove: boolean
}

export interface MarketBreadth {
  region: string
  pctAbove200d: number
  pctAbove50d: number
  advancers: number
  decliners: number
  newHighs: number
  newLows: number
  asOfByRegion: Record<string, ISODate>
}

export interface MarketMover {
  id: string
  ticker: string
  name: string
  region: string
  currency: string
  close: number
  change1d: number
  volumeZ: number | null
  marketCapUsd: number | null
  pxDate: ISODate | null
  /** v1.30: 20-day average traded value (USD) */
  advUsd20d?: number | null
}

/** v1.30 (F21): liquidity screen the API applied to gainers / losers */
export interface MoversFilter {
  liquid: boolean
  minAdvUsd: number | null
  minMcapUsd: number | null
  minPriceUsd: number | null
  unknownMcapMinAdvUsd: number | null
  limit: number | null
}

export interface MarketOverview {
  dataStatus?: DataStatus
  asOf: ISODate
  regime: MacroRegime
  yieldCurves: YieldCurve[]
  indicators: MacroIndicator[]
  creditSpreads: CreditSpread[]
  sectorPerformance: SectorPerformance[]
  /** v1.29: per-region data date; empty on older APIs */
  dataAsOf: RegionDataAsOf[]
  /** v1.29: all regions on the same session (null = unknown) */
  datesAligned: boolean | null
  indices: IndexQuote[]
  /** breadth of the requested region set */
  breadth: MarketBreadth | null
  /** one breadth row per region when "All" is selected */
  breadthByRegion: MarketBreadth[]
  gainers: MarketMover[]
  losers: MarketMover[]
  /** v1.30: server-side movers screen (absent on older APIs → the client-side liquid filter applies) */
  moversFilter?: MoversFilter | null
}

/* ───────────────────────────── sectors ───────────────────────────── */

export interface SubsectorNode {
  id: string
  name: string
  kpiProfile: KpiProfile
  companyCount: number
}

export interface SectorNode {
  id: string
  name: string
  companyCount: number
  subsectors: SubsectorNode[]
}

export interface KpiDefinition {
  id: string
  label: string
  unit: ValueUnit
  /** null = band metric (see target) or neutral */
  higherIsBetter: boolean | null
  /** band metrics: the value scored best */
  target?: number | null
  description: string
}

export interface KpiDistribution {
  kpi: KpiDefinition
  values: { companyId: string; ticker: string; value: number }[]
  p10: number
  p25: number
  median: number
  p75: number
  p90: number
}

export interface SectorDetail {
  dataStatus?: DataStatus
  asOf: ISODate
  sector: string
  subsector: string | null
  kpiProfile: KpiProfile
  kpis: KpiDefinition[]
  distributions: KpiDistribution[]
  stats: { companyCount: number; medianScore: number; totalMarketCapUsd: number; medianReturn1m: number }
  leaders: ScreenerRow[]
  laggards: ScreenerRow[]
}

/* ───────────────────────────── company 360 ───────────────────────────── */

export interface CompanyProfile extends CompanySummary {
  description: string
  website: string
  employees: number
  fiscalYearEnd: string
  reportingCurrency: CurrencyCode
  fxToUsd: number
  high52w: number
  low52w: number
  beta: number
  sharesOutstanding: number
  enterpriseValueUsd: number
  avgDailyValueUsd: number
  lastFilingDate: ISODate
  dataQualityNotes: string[]
  /** ADR / secondary line: scores, technicals and fair value live on the home listing */
  isAdr?: boolean
  homeListing?: { id: string; ticker: string; exchange: string } | null
  /** v1.44 full-universe scores: mvp = MVP model universe; extended = anchored to the MVP reference cross-section */
  scoreScope?: 'mvp' | 'extended' | null
  /** why the composite is empty (insufficient_data with the missing inputs) */
  compositeReason?: AvailabilityReason | null
  /** coverage v3 A8: API confirms this composite lies inside a passed gate's tested population (overview
   *  composite.validated); anything but true → the composite is labelled descriptive */
  compositeValidated?: boolean
  /** rank within region x sector among all scored securities (MVP + extended) */
  rankSectorFull?: number | null
  nSectorFull?: number | null
  /** v1.44: the 3M prediction is an extrapolation of the MVP-trained model */
  mlScope?: 'mvp' | 'extrapolated' | null
}

export interface FactorScoreDetail {
  family: FactorFamily
  score: number
  peerMedian: number
  weight: number
  contribution: number
  subFactors: { name: string; score: number }[]
}

export interface KeyRatio {
  id: string
  label: string
  unit: ValueUnit
  value: number | null
  peerMedian: number | null
  /** 0–100, oriented so that higher = better */
  peerPercentile: number | null
  /** z-score of current value vs the company's own trailing 5y distribution */
  selfZScore5y: number | null
  higherIsBetter: boolean
  history: number[]
}

export type AlertSeverity = 'info' | 'warning' | 'critical'

export interface CompanyAlert {
  id: string
  date: ISODate
  severity: AlertSeverity
  category: 'fundamental' | 'insider' | 'sentiment' | 'technical' | 'model' | 'data'
  message: string
  /** badge id from the API (e.g. beneish_flag) */
  code?: string
  /** model-based screen, not an accounting finding */
  modelBased?: boolean
}

/** v1.29 Beneish M-score detail (MODEL-BASED screen; textbook threshold −1.78). */
export interface BeneishDetail {
  level: 'flag' | 'watch' | 'none' | 'not_applicable'
  mScore: number | null
  mScoreGrowthNeutral: number | null
  threshold: number
  textbookFlag: boolean | null
  growthDriven: boolean | null
  applicable: boolean
  /** index id → value, contribution (weight × (index − neutral)), manipulator-sample mean */
  components: { id: string; label: string; value: number | null; contribution: number | null; manipulatorMean: number | null; redFlag: boolean }[]
  redFlags: string[]
  corroboration: string[]
  method: string | null
  periodEnd: ISODate | null
}

export interface CompanyOverview {
  dataStatus?: DataStatus
  profile: CompanyProfile
  factors: FactorScoreDetail[]
  keyRatios: KeyRatio[]
  alerts: CompanyAlert[]
  priceHistory: PriceBar[]
  /** v1.29 Beneish breakdown, only fetched when a Beneish badge is raised */
  beneish?: BeneishDetail | null
}

/* financials */

export type StatementType = 'income' | 'balance' | 'cashflow'
export type Periodicity = 'annual' | 'quarterly'

export interface FinancialLineItem {
  id: string
  label: string
  /** indentation depth 0–2 */
  level: number
  isTotal: boolean
  /** values aligned to FinancialStatement.periods, in millions of reporting currency; null = not reported */
  values: (number | null)[]
  /** per-share or ratio items are not scaled/converted */
  kind: 'amount' | 'perShare' | 'shares'
}

export interface FinancialStatement {
  type: StatementType
  periodicity: Periodicity
  currency: CurrencyCode
  periods: string[]
  periodEnds: ISODate[]
  /** FX rate (reporting → USD) at each period end */
  fxToUsd: number[]
  items: FinancialLineItem[]
}

export interface CompanyFinancials {
  dataStatus?: DataStatus
  companyId: string
  reportingCurrency: CurrencyCode
  periodicity: Periodicity
  statements: FinancialStatement[]
}

/* forecast */

export type Scenario = 'bear' | 'base' | 'bull'

export interface ForecastSeries {
  metric: 'revenue' | 'ebit' | 'fcf' | 'eps'
  label: string
  unit: ValueUnit
  historical: { period: string; value: number }[]
  projections: Record<Scenario, { period: string; value: number }[]>
}

export interface ForecastAssumption {
  id: string
  label: string
  unit: ValueUnit
  bear: number
  base: number
  bull: number
  historical5y: number | null
}

export interface ProjectedStatementRow {
  id: string
  label: string
  isTotal: boolean
  unit: ValueUnit
  values: number[]
}

export interface ReverseDcf {
  currentPrice: number
  impliedGrowth: number
  impliedYears: number
  wacc: number
  terminalGrowth: number
  historicalGrowth5y: number
  consensusGrowth: number | null
  fairValue: Record<Scenario, number>
  sensitivity: { wacc: number; terminalGrowth: number; fairValue: number }[]
}

export interface CostOfCapital {
  asOf: ISODate | null
  currency: CurrencyCode | null
  wacc: number
  costOfEquity: number
  beta: number
  betaRaw: number
  betaSource: string | null
  rf: number
  rfSource: string | null
  erp: number
  crp: number
  kdPost: number
  taxRate: number
  weightEquity: number
  terminalGrowth: number
  isFinancial: boolean
  flags: string[]
}

export interface FairValueDetail {
  model: 'dcf' | 'excess_return' | null
  /** headline: DCF × peer-multiples blend, shrunk toward price by confidence (market-anchored) */
  blended: number
  upsidePct: number
  dcf: number
  multiples: number
  low: number
  high: number
  target12m: number
  /** 0–100 */
  confidence: number
  confidenceReasons: string[]
  /** terminal value share of EV, 0–1 */
  tvShare: number
  flags: string[]
}

export interface CompanyForecast {
  dataStatus?: DataStatus
  fairValueDetail?: FairValueDetail
  costOfCapital?: CostOfCapital | null
  currency: CurrencyCode
  projectionPeriods: string[]
  series: ForecastSeries[]
  assumptions: ForecastAssumption[]
  projected: Record<Scenario, ProjectedStatementRow[]>
  reverseDcf: ReverseDcf
}

/* peers */

export interface PeerRow {
  companyId: string
  ticker: string
  name: string
  country: CountryCode
  marketCapUsd: number
  compositeScore: number
  isSubject: boolean
  metrics: Record<string, number | null>
}

export interface CompanyPeers {
  dataStatus?: DataStatus
  peerCount?: number
  /** v1.7: subsector / sector / … or peer_set (Model Studio cluster) */
  level?: string
  peerGroupKey?: string
  kpiProfile: KpiProfile
  metrics: KpiDefinition[]
  peers: PeerRow[]
  scatter: { xMetric: string; yMetric: string }
  subjectRankByMetric: Record<string, { rank: number; of: number }>
}

/* moat */

export type MoatSource = 'Intangibles' | 'Switching costs' | 'Network effects' | 'Cost advantage' | 'Efficient scale'

export interface MoatEvidence {
  /** v1.2: supplementary components are shown but weigh less */
  supplementary?: boolean
  /** component score 0–100 (threshold applies to this) */
  score?: number
  id: string
  label: string
  unit: ValueUnit
  value: number
  threshold: number
  passes: boolean
  description: string
  history: TimePoint[]
}

export interface CompanyMoat {
  dataStatus?: DataStatus
  modelVariant?: 'standard' | 'financial' | null
  yearsEvaluated?: number | null
  rating: MoatRating
  score: number
  confidence: number
  trend: 'widening' | 'stable' | 'narrowing'
  sources: { source: MoatSource; strength: number }[]
  evidence: MoatEvidence[]
  roicVsWacc: { date: ISODate; roic: number; wacc: number; hurdle?: number }[]
  marketShare: { date: ISODate; value: number }[]
  summary: string
}

/* insiders */

/** SEC Form 4 transaction codes (P purchase, S sale, A award, M option exercise, F tax withholding, G gift) */
export type InsiderTxnCode = 'P' | 'S' | 'A' | 'M' | 'F' | 'G'

export interface InsiderTransaction {
  id: string
  companyId: string
  ticker: string
  filedAt: ISODateTime
  tradeDate: ISODate
  insiderName: string
  role: string
  code: InsiderTxnCode
  shares: number
  price: number
  valueUsd: number
  sharesOwnedAfter: number
  ownershipChangePct: number
  anomalyScore: number
  isClusterBuy: boolean
  source: string
  clusterId?: string | null
  /** model features behind the anomaly score (tooltip) */
  features?: Record<string, unknown> | null
}

export interface CompanyInsiders {
  dataStatus?: DataStatus
  coverage?: 'full' | 'partial' | 'none'
  anomalyScore: number
  netBuyValue90dUsd: number
  buyers90d: number
  sellers90d: number
  transactions: InsiderTransaction[]
  clusterBuys: { date: ISODate; insiders: number; valueUsd: number }[]
  priceHistory: PriceBar[]
}

export interface InsiderRadarItem extends InsiderTransaction {
  companyName: string
  country: CountryCode
  region: RegionCode
  sector: string
  marketCapUsd: number
  reason: string
}

/* sentiment & events */

export interface SentimentPoint {
  date: ISODate
  sentiment: number // -1 … 1
  newsVolume: number
  close: number
}

export interface NewsItem {
  id: string
  publishedAt: ISODateTime
  source: string
  headline: string
  url: string
  language: string
  sentiment: number // -1 … 1
  relevance: number // 0 … 1
}

export type EventCategory = 'Protest' | 'Policy' | 'Conflict' | 'Economic' | 'Disaster' | 'Diplomacy' | 'Legal' | 'Corporate'

export interface EventItem {
  id: string
  date: ISODate
  category: EventCategory
  headline: string
  countries: CountryCode[]
  tone: number // GDELT average tone, roughly -10 … +10
  intensity: number // 0 … 1
  articleCount: number
}

export interface CompanySentiment {
  dataStatus?: DataStatus
  currentIndex: number
  /** 0–100 cross-sectional sentiment score (percentile blend; 50 = neutral / no data) */
  score?: number | null
  change30d: number
  toneAvg30d: number
  lazyPricesSimilarity: number | null
  index: SentimentPoint[]
  news: NewsItem[]
  events: EventItem[]
}

/* technicals */

export type LevelType =
  | 'swing_pivot'
  | 'volume_profile'
  | 'fibonacci'
  | 'round_number'
  | 'ma_200'
  | 'ma_50'
  | 'gap'
  | 'anchored_vwap'
  | 'prior_high_low'

export interface LevelTypeStat {
  type: LevelType
  label: string
  hitRate: number
  sampleSize: number
  avgReactionPct: number
}

export interface ConfluenceZone {
  id: string
  low: number
  high: number
  kind: 'support' | 'resistance'
  strength: number // 0 … 1
  rank: number
  contributors: LevelType[]
  touches: number
}

export interface SwingPivot {
  time: ISODate
  price: number
  kind: 'high' | 'low'
  /** market-structure label */
  label?: 'HH' | 'HL' | 'LH' | 'LL' | null
}

export interface LevelTypeReliability {
  type: string
  label: string
  nEvents: number
  hitRate: number
  /** hit rate minus random-level control */
  edge: number
}

export interface TradeSetup {
  bias: 'long' | 'short' | 'neutral'
  entry: number
  invalidation: number
  target1: number
  target2: number
  riskReward: number
  zoneId: string | null
  note: string
  entryLow?: number
  entryHigh?: number
  /** 0–100 */
  technicalScore?: number
  scoreComponents?: { key: string; score: number; weight: number }[]
  trend?: 'up' | 'down' | 'sideways' | null
  structureState?: 'bullish' | 'bearish' | 'range' | null
  lastBreak?: { kind: 'bos' | 'choch'; direction: 'up' | 'down' | null; date: ISODate | null; price: number } | null
}

export interface CompanyTechnicals {
  dataStatus?: DataStatus
  /** trading currency of the bars / levels */
  currency?: CurrencyCode
  typeReliability?: LevelTypeReliability[]
  bars: PriceBar[]
  zones: ConfluenceZone[]
  pivots: SwingPivot[]
  levelStats: LevelTypeStat[]
  setup: TradeSetup
  indicators: { rsi14: number; atr14: number; ma50: number; ma200: number; trend: 'up' | 'down' | 'sideways' }
}

/* model */

export type ModelKind = 'xgboost' | 'transformer'

export interface ModelPrediction {
  model: ModelKind
  /** raw model type (xgboost_rank, ensemble, ft_transformer …) */
  modelType?: string
  modelVersion: string
  horizonDays: number
  predictedRankPct: number
  expectedExcessReturn: number
  intervalLow: number
  intervalHigh: number
  confidence: number
  trainedAt: ISODateTime
}

export interface ShapContribution {
  feature: string
  family: FactorFamily
  featureValue: string
  contribution: number
}

export interface CompanyModel {
  dataStatus?: DataStatus
  /** unit of SHAP contributions as published (absent = % excess return) */
  shapUnit?: string | null
  asOf: ISODate
  featureSetVersion: string
  predictions: ModelPrediction[]
  baseValue: number
  shap: ShapContribution[]
  rankHistory: { date: ISODate; xgboost: number; transformer: number }[]
  /** v1.44: extrapolated = outside the MVP model universe (MVP-trained model, rank among MVP predictions) */
  scope?: 'mvp' | 'extrapolated' | null
  scopeNote?: string | null
  /** why there is no prediction (suppressed / insufficient_data / stale) */
  reason?: AvailabilityReason | null
}

/* ───────────────────────────── cross-sectional pages ───────────────────────────── */

export interface CountryEventStat {
  country: CountryCode
  countryName: string
  tone: number
  intensity: number
  eventCount: number
  change7d: number
}

export interface GlobalEvent extends EventItem {
  affectedSectors: string[]
  affectedCompanies: { id: string; ticker: string; name: string; exposure: number }[]
}

export interface GlobalEventsPayload {
  dataStatus?: DataStatus
  asOf: ISODate
  countries: CountryEventStat[]
  timeline: { date: ISODate; tone: number; eventCount: number }[]
  events: GlobalEvent[]
}

export type RunStatus = 'success' | 'failed' | 'running' | 'queued' | 'skipped' | 'partial'

export interface ModelRun {
  id: string
  model: ModelKind
  version: string
  status: RunStatus
  startedAt: ISODateTime
  durationSec: number
  trainStart: ISODate
  trainEnd: ISODate
  testStart: ISODate
  testEnd: ISODate
  ic: number
  rankIc: number
  icir: number
  hitRate: number
  decileSpreadAnn: number
  nFeatures: number
  promoted: boolean
  /** contract model_type (xgboost_rank | ft_transformer | cs_attention | ensemble) and horizon */
  modelType?: string
  horizon?: string | null
  /** v1.39.0 label-permutation leakage gate: failed = demoted, comparison only */
  leakageGate?: 'passed' | 'failed' | null
  gateNote?: string | null
}

export interface ModelLabPayload {
  dataStatus?: DataStatus
  runs: ModelRun[]
  icSeries: { date: ISODate; xgboostIc: number; xgboostRankIc: number; transformerIc: number; transformerRankIc: number }[]
  deciles: { decile: number; xgboost: number; transformer: number }[]
  cumulativeSpread: { date: ISODate; xgboost: number; transformer: number; benchmark: number }[]
  /** familyLabel = model-internal feature family as published (e.g. "adv", "macro") */
  featureImportance: { feature: string; family: FactorFamily; familyLabel?: string; importance: number }[]
  folds: { fold: number; trainStart: ISODate; trainEnd: ISODate; testStart: ISODate; testEnd: ISODate; rankIc: number; sharpe: number }[]
  /** runs behind the charts: horizon and leakage gate of each family (guide 10) */
  xgboostRun?: { id: string; modelType: string; horizon: string | null; leakageGate: 'passed' | 'failed' | null; gateNote: string | null } | null
  transformerRun?: { id: string; modelType: string; horizon: string | null; leakageGate: 'passed' | 'failed' | null; gateNote: string | null } | null
}

export interface JobRun {
  id: string
  job: string
  source: string
  status: RunStatus
  startedAt: ISODateTime
  durationSec: number
  rowsWritten: number
  message: string
}

export interface SourceFreshness {
  source: string
  description: string
  region: RegionCode | 'GLOBAL'
  cadence: 'intraday' | 'daily' | 'weekly' | 'monthly' | 'quarterly' | 'manual'
  dqStatus?: 'ok' | 'warn' | 'error' | 'no_data' | 'info' | null
  jobId?: string | null
  lastSuccess: ISODateTime
  lagHours: number
  status: 'fresh' | 'stale' | 'error'
  coverage: number
}

export interface SystemStatus {
  asOf: ISODateTime
  jobs: JobRun[]
  sources: SourceFreshness[]
  /** v1.28 data ops: raw contract blocks (see api/ops/types.ts) */
  scheduler?: import('./ops/types').OpsSchedulerStatus | null
  dataAsOf?: Record<string, ISODate>
  dataAsOfOverall?: ISODate | null
  servingSections?: import('./ops/types').OpsServingSection[]
  opsAlerts?: import('./ops/types').OpsAlert[]
  /** coverage v3 A7: background status cache (null on older backends) */
  statusCache?: import('./ops/types').StatusCacheView | null
}

export interface FreshnessSummary {
  /** DATA as-of date (most recent core-region as-of, v1.28) - not the snapshot rebuild time */
  asOf: ISODate
  /** snapshot rebuild time */
  lastUpdate: ISODateTime
  status: 'fresh' | 'stale' | 'error'
  staleSources: string[]
  /** per-region data as-of (v1.28) */
  regions?: Record<string, ISODate>
  schedulerOn?: boolean | null
  profile?: string | null
  /** warning chip text ('' = none): Scheduler off / Worker off / Data stale */
  chip?: string
  chipTone?: 'warn' | 'down' | null
  alerts?: { id: string; severity: 'error' | 'warn' | 'info'; title: string; detail?: string | null; since?: string | null; link?: string | null }[]
}

/* ───────────────────────────── client-side (persisted UI) ───────────────────────────── */

/** studioDrift: Model Studio scheduled-study drift (v1.7, created by scheduling a study — not from this page) */
/** earningsUpcoming: threshold = days ahead; earningsSurprise: threshold = |model surprise z| (v1.22, model-based, not consensus) */
/** backtestDrift: Backtest Lab paper-strategy drift / decay (v1.24, created by scheduling a strategy weekly) */
/** activist13d: new 13D / 13G -> 13D switch (SEC); siSpike: threshold = min short-interest increase %; euShort: threshold = min net short % (v1.25) */
export type AlertMetric = 'price' | 'change1dPct' | 'compositeScore' | 'insiderAnomaly' | 'sentiment' | 'zoneTouch' | 'studioDrift' | 'backtestDrift' | 'earningsUpcoming' | 'earningsSurprise' | 'activist13d' | 'siSpike' | 'euShort' | 'cfOutsideRange' | 'cfPeerAnomaly' | MacroAlertMetric
/** Macro v2 country-level rules (no security target; events fire as security_id "macro:<CC>") */
export type MacroAlertMetric = 'macroRelease' | 'macroSurprise' | 'regimeChange'
export type AlertOperator = '>' | '<' | 'crosses'

export interface AlertRule {
  id: string
  companyId: string
  ticker: string
  metric: AlertMetric
  operator: AlertOperator
  threshold: number
  enabled: boolean
  createdAt: ISODateTime
  /** studioDrift rules (backtestDrift: strategy id / name) */
  studyId?: string
  studyName?: string
  /** macro rules: contract params (countries, max_importance, threshold_z, themes, kind, level …) */
  macroParams?: Record<string, unknown>
  /** server rule type and params (v1.39.0 display: rules created through the API carry no ui_* keys) */
  ruleType?: string
  params?: Record<string, unknown>
  /** v1.39.0 resolved display names (Alert.security_name / watchlist_id / watchlist_name) */
  name?: string | null
  watchlistId?: string | null
  watchlistName?: string | null
}

export interface AlertEventView {
  id: string
  alertId: string
  target: string
  firedAt: ISODateTime
  asOfDate: string
  message: string
  read: boolean
}

/* ───────────────────────────── backend metadata (System page) ───────────────────────────── */

export interface BackendHealth {
  status: 'ok' | 'degraded'
  version: string
  time: ISODateTime
  servingRunId: string | null
  servingBuiltAt: ISODateTime | null
  /** which API keys are configured (booleans only — never values) */
  keysPresent: Record<string, boolean>
  universeCounts: Record<string, number>
  diskUsageGb: number | null
}

export interface FactorDefinition {
  family: FactorFamily
  name: string
  description: string
  memberCount: number
}

export interface FactorModelMeta {
  defaultProfile: string
  /** profile id → weights (fractions summing to ~1) */
  profiles: Record<string, FactorWeights>
  factors: FactorDefinition[]
  /** coverage v3 A8: descriptive unless a recorded out-of-sample gate passed (GET /meta/factors composite_validation) */
  compositeValidation: CompositeValidationView
}

export type CompositeLabelKind = 'descriptive' | 'validated'

export interface CompositeProfileValidation {
  label: CompositeLabelKind
  verdict: 'pass' | 'fail' | 'not_validated'
  /** true only behind a passed out-of-sample gate: "signal" / "predictive" / buy-sell wording allowed */
  signalWording: boolean
  horizon: string | null
  rankIcMean: number | null
  rankIcT: number | null
  aucMean: number | null
  aucT: number | null
  /** scoring months of the gate panel (one scoring date per month) */
  nDates: number | null
  /** population the gate tested: a pass applies only to composites inside it (API-confirmed per composite) */
  universe?: CompositeGateUniverseView | null
}

export interface CompositeGateUniverseView {
  table: string | null
  scopes: ('mvp' | 'extended')[]
  /** issuer regions of the tested securities (informational: US-listed ADRs carry their issuer's region) */
  regions: string[]
  nSecurities: number | null
}

/** Coverage v3 A8: composite scores are a descriptive, peer-relative ranking; the API says when (if ever) a profile
 *  passed its recorded out-of-sample gate (rank IC and AUC with Newey-West t >= 2). */
export interface CompositeValidationView {
  label: CompositeLabelKind
  defaultProfile: string
  signalWording: boolean
  note: string
  evaluatedAt: string | null
  profiles: Record<string, CompositeProfileValidation>
}
