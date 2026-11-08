import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { thesesApi } from './client'
import type { JournalInput, ReviewInput, SubjectType, ThesisCreate, ThesisFilters, ThesisUpdate } from './types'

export const tk = {
  all: ['theses'] as const,
  list: (f: ThesisFilters) => ['theses', 'list', f] as const,
  stats: () => ['theses', 'stats'] as const,
  defaults: (t: string, id: string) => ['theses', 'defaults', t, id] as const,
  detail: (id: string) => ['theses', 'detail', id] as const,
  sub: (id: string, what: string) => ['theses', 'detail', id, what] as const,
}

const S = 15_000

export const useTheses = (f: ThesisFilters) => useQuery({ queryKey: tk.list(f), queryFn: () => thesesApi.list(f), staleTime: S, placeholderData: keepPreviousData })
export const useThesisStats = () => useQuery({ queryKey: tk.stats(), queryFn: () => thesesApi.stats(), staleTime: S })
export const useSubjectDefaults = (t: SubjectType | '', id: string) =>
  useQuery({ queryKey: tk.defaults(t, id), queryFn: () => thesesApi.defaults(t as SubjectType, id), enabled: !!t && id.trim().length > 0, staleTime: 60_000, retry: false })
export const useThesis = (id: string) => useQuery({ queryKey: tk.detail(id), queryFn: () => thesesApi.get(id), staleTime: S, enabled: !!id })
export const useThesisStatus = (id: string) => useQuery({ queryKey: tk.sub(id, 'status'), queryFn: () => thesesApi.status(id), staleTime: S })
export const useThesisPerformance = (id: string) => useQuery({ queryKey: tk.sub(id, 'performance'), queryFn: () => thesesApi.performance(id), staleTime: S })
export const useThesisTimeline = (id: string) => useQuery({ queryKey: tk.sub(id, 'timeline'), queryFn: () => thesesApi.timeline(id), staleTime: S })
export const useThesisEvidence = (id: string) => useQuery({ queryKey: tk.sub(id, 'evidence'), queryFn: () => thesesApi.evidence(id), staleTime: 60_000 })
export const useThesisJournal = (id: string) => useQuery({ queryKey: tk.sub(id, 'journal'), queryFn: () => thesesApi.journal(id), staleTime: S })
export const useThesisReviews = (id: string) => useQuery({ queryKey: tk.sub(id, 'reviews'), queryFn: () => thesesApi.reviews(id), staleTime: S })

/** Every write touches status / alerts / snapshots: invalidate the whole thesis subtree + the board. */
function useInvalidate() {
  const qc = useQueryClient()
  return () => qc.invalidateQueries({ queryKey: tk.all })
}

export function useThesisMutations(id: string) {
  const inv = useInvalidate()
  return {
    update: useMutation({ mutationFn: (p: ThesisUpdate) => thesesApi.update(id, p), onSuccess: inv }),
    remove: useMutation({ mutationFn: () => thesesApi.remove(id), onSuccess: inv }),
    addJournal: useMutation({ mutationFn: (b: JournalInput) => thesesApi.addJournal(id, b), onSuccess: inv }),
    updateJournal: useMutation({ mutationFn: (a: { entryId: string; body: Partial<JournalInput> }) => thesesApi.updateJournal(id, a.entryId, a.body), onSuccess: inv }),
    deleteJournal: useMutation({ mutationFn: (entryId: string) => thesesApi.deleteJournal(id, entryId), onSuccess: inv }),
    review: useMutation({ mutationFn: (b: ReviewInput) => thesesApi.completeReview(id, b), onSuccess: inv }),
    snapshot: useMutation({ mutationFn: (note?: string) => thesesApi.snapshot(id, note), onSuccess: inv }),
    evaluate: useMutation({ mutationFn: () => thesesApi.evaluate(id), onSuccess: inv }),
  }
}

export function useCreateThesis() {
  const inv = useInvalidate()
  return useMutation({ mutationFn: (b: ThesisCreate) => thesesApi.create(b), onSuccess: inv })
}

/** Board moves (any thesis). */
export function useMoveThesis() {
  const inv = useInvalidate()
  return useMutation({ mutationFn: (a: { id: string; patch: ThesisUpdate }) => thesesApi.update(a.id, a.patch), onSuccess: inv })
}
