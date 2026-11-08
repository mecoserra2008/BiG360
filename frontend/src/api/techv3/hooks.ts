import { useQuery } from '@tanstack/react-query'
import { USE_MOCK } from '../http'
import { techV3Api } from './client'

const opts = { staleTime: 10 * 60_000, retry: 1 } as const

export const useTechFactors = (id: string, enabled = true) =>
  useQuery({ queryKey: ['technicals', 'factors', id] as const, queryFn: () => techV3Api.factors(id), enabled: enabled && !!id && !USE_MOCK, ...opts })

export const useTechBreakout = (id: string, enabled = true) =>
  useQuery({ queryKey: ['technicals', 'breakout', id] as const, queryFn: () => techV3Api.breakout(id), enabled: enabled && !!id && !USE_MOCK, ...opts })
