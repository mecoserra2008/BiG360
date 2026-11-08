/** Markets → Cross-asset (contract v1.23.0): per-asset trend / carry / value / curve / vol / CFTC positioning signals,
 * Yahoo futures term structures, correlation regime and the model-based risk-on / risk-off gauge. */
import type { components } from '../generated/schema'

type S = components['schemas']
export type XaClass = S['CrossAssetClass']
export type XaLabels = S['CrossAssetLabels']
export type XaRef = S['CrossAssetRef']
export type XaPositioning = S['CrossAssetPositioning']
export type XaSignalDef = S['CrossAssetSignalDef']
export type XaCockpitRow = S['CrossAssetCockpitRow']
export type XaCockpit = S['CrossAssetCockpit']
export type XaAssetDetail = S['CrossAssetAssetDetail']
export type XaCurveList = S['CrossAssetCurveList']
export type XaCurveListItem = XaCurveList['items'][number]
export type XaCurvePoint = S['CrossAssetCurvePoint']
export type XaCurve = S['CrossAssetCurve']
export type XaCurveSnapshot = XaCurve['snapshots'][number]
export type XaCoverage = XaCurve['coverage'][number]
export type XaCorrelation = S['CrossAssetCorrelation']
export type XaRisk = S['CrossAssetRisk']
export type XaRiskComponent = S['CrossAssetRiskComponent']
export type XaSnapshotLabel = 'latest' | '1m' | '1y'
export type XaWindow = 60 | 120

export const CLASS_LABEL: Record<XaClass, string> = {
  commodity: 'Commodities',
  fx: 'FX',
  equity_index: 'Equity indices',
  rates: 'US Treasuries',
  credit: 'US credit',
  crypto: 'Crypto',
}
export const SNAPSHOT_LABEL: Record<XaSnapshotLabel, string> = { latest: 'Now', '1m': '~1m ago', '1y': '~1y ago' }
export const COT_CATEGORY_LABEL: Record<string, string> = { m_money: 'managed money', lev_money: 'leveraged funds' }
export const COT_REPORT_LABEL: Record<string, string> = { disagg_fut: 'Disaggregated, futures only', tff_fut: 'TFF, futures only' }
export const REGIME_LABEL: Record<string, string> = { risk_on: 'Risk-on', neutral: 'Neutral', risk_off: 'Risk-off' }
export const SOURCE_YAHOO = 'Yahoo market data · expiry dates approximate'
