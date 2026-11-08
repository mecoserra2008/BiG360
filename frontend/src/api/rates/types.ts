/** Rates & debt (contract v1.47.0, docs/COVERAGE_V2_PLAN.md W3): official curves, Curve monitor, sovereign debt,
 * credit and the Company 360 Debt panel. Rates are percent, spreads basis points. */
import type { components } from '../generated/schema'

type S = components['schemas']
export type RatesCurveList = S['RatesCurveList']
export type RatesCurveMeta = S['RatesCurveMeta']
export type RatesCurve = S['RatesCurve']
export type RatesCurvePoint = S['RatesCurvePoint']
export type RatesSnapshot = RatesCurve['snapshots'][number]
export type RatesFrames = NonNullable<RatesCurve['frames']>
export type RatesMonitor = S['RatesMonitor']
export type RatesMonitorCountry = S['RatesMonitorCountry']
export type RatesSeries = S['RatesSeries']
export type RatesSovereign = S['RatesSovereign']
export type RatesAuction = S['RatesAuction']
export type CreditOverview = S['CreditOverview']
export type CreditSeries = CreditOverview['series'][number]
export type CreditWallBucket = S['CreditWallBucket']
export type CreditMaturityWalls = S['CreditMaturityWalls']
export type CompanyDebt = S['CompanyDebt']
export type CompanyDebtPeriod = S['CompanyDebtPeriod']
export type CurveType = 'par' | 'spot' | 'forward' | 'yield'

export const TYPE_LABEL: Record<CurveType, string> = { par: 'Par', spot: 'Spot (zero)', forward: 'Instantaneous forward', yield: 'Benchmark yield' }
export const KIND_LABEL: Record<string, string> = { nominal: 'Nominal', real: 'Real (inflation-linked)', inflation: 'Implied inflation', ois: 'OIS', money_market: 'Money market' }
export const BUCKET_LABEL: Record<string, string> = { y1: '< 1y', y2: '1-2y', y3: '2-3y', y4: '3-4y', y5: '4-5y', after5: '> 5y', y2_5: '1-5y', total: 'Total' }
export const CREDIT_GROUP_LABEL: Record<string, string> = { rating: 'By rating', maturity: 'By maturity (US IG)', sector: 'By sector (EM)', region: 'By region', yield: 'Effective yields', moodys: "Moody's" }
export const SNAP_LABEL: Record<string, string> = { latest: 'Latest', '1d': '1 day ago', '1w': '1 week ago', '1m': '1 month ago', '3m': '3 months ago', '6m': '6 months ago', '1y': '1 year ago', '3y': '3 years ago', '5y': '5 years ago' }
