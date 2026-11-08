import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { type MarketsQuery, positioningApi } from './client'
import type { PosFamily, PosMetric } from './types'

const H = 30 * 60_000
export const usePosMarkets = (q: MarketsQuery, enabled = true) =>
  useQuery({ queryKey: ['positioning', 'markets', q], queryFn: () => positioningApi.markets(q), staleTime: H, enabled })
export const usePosHeatmap = (report: PosFamily, metric: PosMetric, combined: boolean, assetClass?: string) =>
  useQuery({ queryKey: ['positioning', 'heatmap', report, metric, combined, assetClass ?? ''], queryFn: () => positioningApi.heatmap(report, metric, combined, assetClass), staleTime: H, placeholderData: keepPreviousData })
export const usePosCrowded = (combined: boolean, assetClass?: string) =>
  useQuery({ queryKey: ['positioning', 'crowded', combined, assetClass ?? ''], queryFn: () => positioningApi.crowded(combined, assetClass), staleTime: H, placeholderData: keepPreviousData })
export const usePosMarket = (code: string, report: PosFamily | null, combined: boolean, from: string | null) =>
  useQuery({ queryKey: ['positioning', 'market', code, report ?? 'auto', combined, from ?? 'max'], queryFn: () => positioningApi.market(code, report, combined, from), staleTime: H, placeholderData: keepPreviousData })
