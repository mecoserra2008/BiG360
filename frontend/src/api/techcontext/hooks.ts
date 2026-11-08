import { useQuery } from '@tanstack/react-query'
import { techContextApi } from './client'

export const useTechContext = (id: string, enabled = true) =>
  useQuery({ queryKey: ['technicals', 'context', id] as const, queryFn: () => techContextApi.context(id), staleTime: 10 * 60_000, enabled: enabled && !!id, retry: 1 })
