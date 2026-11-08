import { keepPreviousData, useQuery } from '@tanstack/react-query'
import type { FactorWeights, Periodicity, RegionFilter } from './types'
import { api } from './client'

/** Query-key factory — keeps cache keys consistent between hooks and invalidations. */
export const qk = {
  market: (region: RegionFilter) => ['market', region] as const,
  sectorTree: (region: RegionFilter) => ['sectors', region] as const,
  sector: (s: string, sub: string | null, region: RegionFilter) => ['sector', s, sub, region] as const,
  screener: (region: RegionFilter, weights?: string) => (weights ? (['screener', region, weights] as const) : (['screener', region] as const)),
  search: (q: string) => ['search', q] as const,
  company: (id: string, part: string, extra?: string) => ['company', id, part, extra ?? ''] as const,
  insiderRadar: (region: RegionFilter) => ['insiderRadar', region] as const,
  events: (region: RegionFilter) => ['events', region] as const,
  modelLab: () => ['modelLab'] as const,
  system: () => ['system'] as const,
  freshness: () => ['freshness'] as const,
  health: () => ['health'] as const,
  factorModel: () => ['factorModel'] as const,
}

export const useMarketOverview = (region: RegionFilter) =>
  useQuery({ queryKey: qk.market(region), queryFn: () => api.marketOverview(region), placeholderData: keepPreviousData })

export const useSectorTree = (region: RegionFilter) =>
  useQuery({ queryKey: qk.sectorTree(region), queryFn: () => api.sectorTree(region), placeholderData: keepPreviousData })

export const useSectorDetail = (sector: string, sub: string | null, region: RegionFilter) =>
  useQuery({ queryKey: qk.sector(sector, sub, region), queryFn: () => api.sectorDetail(sector, sub, region), placeholderData: keepPreviousData })

/** With `weights`, the server selects the top 1,000 under those weights (the key changes with them, so a weight
 *  change refetches; the previous rows stay on screen meanwhile). */
export const useScreener = (region: RegionFilter, weights?: FactorWeights) =>
  useQuery({
    queryKey: qk.screener(region, weights ? JSON.stringify(weights) : undefined),
    queryFn: () => api.screener(region, weights ? { weights } : undefined),
    placeholderData: keepPreviousData,
  })

export const useSearch = (q: string) =>
  useQuery({ queryKey: qk.search(q), queryFn: () => api.search(q), enabled: q.trim().length > 0, staleTime: 60_000, placeholderData: keepPreviousData })

export const useCompanyOverview = (id: string) => useQuery({ queryKey: qk.company(id, 'overview'), queryFn: () => api.companyOverview(id) })
export const useCompanyFinancials = (id: string, p: Periodicity) =>
  useQuery({ queryKey: qk.company(id, 'financials', p), queryFn: () => api.companyFinancials(id, p), placeholderData: keepPreviousData })
export const useCompanyForecast = (id: string) => useQuery({ queryKey: qk.company(id, 'forecast'), queryFn: () => api.companyForecast(id) })
export const useCompanyPeers = (id: string, peerSetId: string | null = null) =>
  useQuery({ queryKey: qk.company(id, 'peers', peerSetId ?? ''), queryFn: () => api.companyPeers(id, peerSetId), placeholderData: keepPreviousData })
export const useCompanyMoat = (id: string) => useQuery({ queryKey: qk.company(id, 'moat'), queryFn: () => api.companyMoat(id) })
export const useCompanyInsiders = (id: string) => useQuery({ queryKey: qk.company(id, 'insiders'), queryFn: () => api.companyInsiders(id) })
export const useCompanySentiment = (id: string) => useQuery({ queryKey: qk.company(id, 'sentiment'), queryFn: () => api.companySentiment(id) })
export const useCompanyTechnicals = (id: string) => useQuery({ queryKey: qk.company(id, 'technicals'), queryFn: () => api.companyTechnicals(id) })
export const useCompanyModel = (id: string) => useQuery({ queryKey: qk.company(id, 'model'), queryFn: () => api.companyModel(id) })

export const useInsiderRadar = (region: RegionFilter) =>
  useQuery({ queryKey: qk.insiderRadar(region), queryFn: () => api.insiderRadar(region), placeholderData: keepPreviousData })
export const useGlobalEvents = (region: RegionFilter) =>
  useQuery({ queryKey: qk.events(region), queryFn: () => api.globalEvents(region), placeholderData: keepPreviousData })
export const useModelLab = () => useQuery({ queryKey: qk.modelLab(), queryFn: () => api.modelLab() })
export const useSystemStatus = () => useQuery({ queryKey: qk.system(), queryFn: () => api.systemStatus(), refetchInterval: 60_000 })
export const useFreshness = () => useQuery({ queryKey: qk.freshness(), queryFn: () => api.freshness(), refetchInterval: 60_000 })
export const useHealth = () => useQuery({ queryKey: qk.health(), queryFn: () => api.health(), refetchInterval: 60_000 })
export const useFactorModel = () => useQuery({ queryKey: qk.factorModel(), queryFn: () => api.factorModel(), staleTime: Infinity })
