import { useQuery } from '@tanstack/react-query'
import { USE_MOCK } from '../http'
import { techMetaApi } from './client'

/** GET /technicals/{id}/meta-setup (the Technicals tab reads the same rows from setup-probabilities.meta). */
export const useMetaSetup = (id: string, horizon = 20, enabled = true) =>
  useQuery({ queryKey: ['technicals', 'meta-setup', id, horizon] as const, queryFn: () => techMetaApi.metaSetup(id, horizon), enabled: enabled && !!id && !USE_MOCK, staleTime: 10 * 60_000, retry: 1 })
