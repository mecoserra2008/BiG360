import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { earningsApi } from './client'
import type { CalendarQuery } from './types'

const M = 10 * 60_000

export const ek = {
  calendar: (q: CalendarQuery) => ['earnings', 'calendar', q] as const,
  company: (id: string) => ['earnings', 'company', id] as const,
  season: (season: string, region: string) => ['earnings', 'season', season, region] as const,
}

export const useEarningsCalendar = (q: CalendarQuery, enabled = true) =>
  useQuery({ queryKey: ek.calendar(q), queryFn: () => earningsApi.calendar(q), staleTime: M, placeholderData: keepPreviousData, enabled })
export const useCompanyEarnings = (id: string) => useQuery({ queryKey: ek.company(id), queryFn: () => earningsApi.company(id), staleTime: M, retry: 1 })
export const useEarningsSeason = (season = '', region = '') =>
  useQuery({ queryKey: ek.season(season, region), queryFn: () => earningsApi.season(season, region), staleTime: M, placeholderData: keepPreviousData })
