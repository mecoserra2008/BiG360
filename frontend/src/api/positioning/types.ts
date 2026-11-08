/** Markets → Positioning (CFTC) (contract v1.21.0): Commitments of Traders positioning across futures markets. */
import type { components } from '../generated/schema'

type S = components['schemas']
export type PosItem = S['PositioningItem']
export type PosMarketList = S['PositioningMarketList']
export type PosHeatmap = S['PositioningHeatmap']
export type PosCrowded = S['PositioningCrowded']
export type PosDetail = S['PositioningMarketDetail']
export type PosSeries = S['PositioningSeries']
export type PosLatest = S['PositioningLatest']
export type PosEventStat = S['PositioningEventStat']
export type PosAssetClass = S['PositioningAssetClass']
export type PosFamily = 'legacy' | 'disagg' | 'tff' | 'cit'
export type PosMetric = 'cot_index_3y' | 'cot_index_1y' | 'z_3y' | 'z_1y' | 'net_pct_oi' | 'pct_rank_3y'

export const FAMILY_LABEL: Record<PosFamily, string> = { legacy: 'Legacy', disagg: 'Disaggregated', tff: 'Financial (TFF)', cit: 'Supplemental (CIT)' }
export const FAMILY_HINT: Record<PosFamily, string> = {
  legacy: 'All markets: commercials vs non-commercials',
  disagg: 'Physical commodities: producers, swap dealers, managed money',
  tff: 'Financial futures: dealers, asset managers, leveraged funds',
  cit: '13 agricultural markets: commodity index traders',
}
export const ASSET_CLASS_LABEL: Record<string, string> = {
  energy: 'Energy', metals: 'Metals', ags: 'Grains & oilseeds', softs: 'Softs', livestock: 'Livestock & dairy',
  rates: 'Rates', equity: 'Equity & vol', fx: 'Currencies', crypto: 'Crypto', other: 'Other',
}
export const METRIC_LABEL: Record<PosMetric, string> = {
  cot_index_3y: 'COT index 3y', cot_index_1y: 'COT index 1y', z_3y: 'z-score 3y', z_1y: 'z-score 1y', net_pct_oi: 'Net % OI', pct_rank_3y: 'Pct rank 3y',
}
export const SOURCE_LABEL = 'CFTC (official)'
