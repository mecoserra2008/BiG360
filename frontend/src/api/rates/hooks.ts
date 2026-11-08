import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { type CurveQuery, ratesApi } from './client'

const H = 30 * 60_000
const LIVE = 60_000

export const useRatesCurves = (group?: string) => useQuery({ queryKey: ['rates', 'curves', group ?? ''], queryFn: () => ratesApi.curves(group), staleTime: H })
export const useRatesCurve = (id: string | null, q: CurveQuery = {}) =>
  useQuery({ queryKey: ['rates', 'curve', id ?? '', q], queryFn: () => ratesApi.curve(id as string, q), staleTime: H, placeholderData: keepPreviousData, enabled: !!id })
/** The monitor carries the delayed live yields: refetched every minute while the page is open. */
export const useRatesMonitor = (tenor = 120) =>
  useQuery({ queryKey: ['rates', 'monitor', tenor], queryFn: () => ratesApi.monitor(tenor), staleTime: LIVE, refetchInterval: LIVE, placeholderData: keepPreviousData })
export const useRatesSeries = (ids: string[], spreads: string[], from?: string | null, to?: string | null) =>
  useQuery({
    queryKey: ['rates', 'series', ids, spreads, from ?? '', to ?? ''],
    queryFn: () => ratesApi.series(ids, spreads, from, to),
    staleTime: H,
    placeholderData: keepPreviousData,
    enabled: ids.length + spreads.length > 0,
  })
export const useRatesSovereign = () => useQuery({ queryKey: ['rates', 'sovereign'], queryFn: () => ratesApi.sovereign(), staleTime: H })
export const useCreditOverview = (years = 5) => useQuery({ queryKey: ['credit', 'overview', years], queryFn: () => ratesApi.credit(years), staleTime: H, placeholderData: keepPreviousData })
export const useCreditWalls = (region = 'ALL', sectorId = 'ALL') =>
  useQuery({ queryKey: ['credit', 'walls', region, sectorId], queryFn: () => ratesApi.walls(region, sectorId), staleTime: H, placeholderData: keepPreviousData })
export const useCompanyDebt = (securityId: string) => useQuery({ queryKey: ['company', securityId, 'debt'], queryFn: () => ratesApi.companyDebt(securityId), staleTime: H })
