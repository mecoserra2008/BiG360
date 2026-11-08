import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { smartMoneyApi } from './client'
import type { ActivismQuery, EuShortQuery } from './types'

const M = 10 * 60_000

export const smk = {
  overview: (region: string) => ['smart-money', 'overview', region] as const,
  euShorts: (q: EuShortQuery) => ['smart-money', 'eu-shorts', q] as const,
  activism: (q: ActivismQuery) => ['smart-money', 'activism', q] as const,
  sectors: (region: string) => ['smart-money', 'sectors', region] as const,
  ownership: (id: string) => ['smart-money', 'ownership', id] as const,
}

export const useSmartMoneyOverview = (region = '') =>
  useQuery({ queryKey: smk.overview(region), queryFn: () => smartMoneyApi.overview(region), staleTime: M, placeholderData: keepPreviousData })
export const useEuShorts = (q: EuShortQuery) =>
  useQuery({ queryKey: smk.euShorts(q), queryFn: () => smartMoneyApi.euShorts(q), staleTime: M, placeholderData: keepPreviousData })
export const useActivism = (q: ActivismQuery) =>
  useQuery({ queryKey: smk.activism(q), queryFn: () => smartMoneyApi.activism(q), staleTime: M, placeholderData: keepPreviousData })
export const useSmartMoneySectors = (region = '') =>
  useQuery({ queryKey: smk.sectors(region), queryFn: () => smartMoneyApi.sectors(region), staleTime: M, placeholderData: keepPreviousData })
export const useCompanyOwnership = (id: string) =>
  useQuery({ queryKey: smk.ownership(id), queryFn: () => smartMoneyApi.ownership(id), staleTime: M, retry: 1 })
