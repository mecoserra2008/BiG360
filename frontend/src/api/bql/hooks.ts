import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { bqlApi } from './client'
import type { BqlSavedInput } from './types'

export const bk = {
  all: ['bql'] as const,
  fields: (q: string, group: string) => ['bql', 'fields', q, group] as const,
  saved: () => ['bql', 'saved'] as const,
  resolve: (q: string) => ['bql', 'resolve', q] as const,
}

export const useBqlFields = (q: string, group = '', limit = 50) =>
  useQuery({ queryKey: [...bk.fields(q, group), limit], queryFn: () => bqlApi.fields(q, group, limit), staleTime: 300_000, placeholderData: keepPreviousData })

export const useBqlSaved = () => useQuery({ queryKey: bk.saved(), queryFn: () => bqlApi.saved(), staleTime: 15_000 })

export const useBqlResolve = (q: string) =>
  useQuery({ queryKey: bk.resolve(q), queryFn: () => bqlApi.resolve(q), enabled: q.trim().length > 0, staleTime: 300_000, retry: false })

export function useBqlSavedMutations() {
  const qc = useQueryClient()
  const inv = () => qc.invalidateQueries({ queryKey: bk.saved() })
  return {
    save: useMutation({ mutationFn: (b: BqlSavedInput) => bqlApi.save(b), onSuccess: inv }),
    update: useMutation({ mutationFn: (a: { id: string; body: Partial<BqlSavedInput> }) => bqlApi.update(a.id, a.body), onSuccess: inv }),
    remove: useMutation({ mutationFn: (id: string) => bqlApi.remove(id), onSuccess: inv }),
  }
}
