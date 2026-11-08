import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { PosCrowded, PosDetail, PosFamily, PosHeatmap, PosMarketList, PosMetric } from './types'

function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/positioning`, 'operation Positioning needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

export interface MarketsQuery {
  asset_class?: string
  report?: 'auto' | PosFamily
  combined?: boolean
  active?: boolean | null
  major?: boolean | null
  security_id?: string
  q?: string
  limit?: number
}

export const positioningApi = {
  async markets(q: MarketsQuery = {}): Promise<PosMarketList> {
    live()
    return http.get('/positioning/markets', { ...q, active: q.active === null ? undefined : q.active })
  },
  async heatmap(report: PosFamily, metric: PosMetric, combined = false, assetClass?: string): Promise<PosHeatmap> {
    live()
    return http.get('/positioning/heatmap', { report, metric, combined, asset_class: assetClass })
  },
  async crowded(combined = false, assetClass?: string): Promise<PosCrowded> {
    live()
    return http.get('/positioning/crowded', { combined, asset_class: assetClass, major: true, limit: 12 })
  },
  async market(code: string, report: PosFamily | null, combined: boolean, from: string | null): Promise<PosDetail> {
    live()
    return http.get(`/positioning/markets/${encodeURIComponent(code)}`, { report: report ?? undefined, combined, from: from ?? undefined })
  },
}
