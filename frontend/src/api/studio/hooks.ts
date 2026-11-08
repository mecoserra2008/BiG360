import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { isTerminal } from './adapters'
import { type VariableQuery, studioApi } from './client'
import type { CGuideQuestion, CModelUpdate, CPromoteRequest, CStudySpec } from './types'

export const sk = {
  catalog: () => ['studio', 'catalog'] as const,
  methods: () => ['studio', 'methods'] as const,
  presets: () => ['studio', 'presets'] as const,
  variables: (q: VariableQuery) => ['studio', 'variables', q] as const,
  runs: () => ['studio', 'runs'] as const,
  run: (id: string) => ['studio', 'run', id] as const,
  studies: () => ['studio', 'studies'] as const,
  study: (id: string) => ['studio', 'study', id] as const,
  peerSets: () => ['studio', 'peer-sets'] as const,
  estimate: (specJson: string) => ['studio', 'estimate', specJson] as const,
  lane: () => ['studio', 'lane'] as const,
  compare: (ids: string[], benchmark: string | null) => ['studio', 'compare', ids, benchmark] as const,
  registry: () => ['studio', 'registry'] as const,
  model: (id: string) => ['studio', 'registry', id] as const,
}

const DAY = 24 * 3600_000

export const useStudioCatalog = () => useQuery({ queryKey: sk.catalog(), queryFn: () => studioApi.catalog(), staleTime: DAY })
export const useStudioMethods = () => useQuery({ queryKey: sk.methods(), queryFn: () => studioApi.methods(), staleTime: DAY })
export const useStudioPresets = () => useQuery({ queryKey: sk.presets(), queryFn: () => studioApi.presets(), staleTime: DAY })

export const useStudioVariables = (q: VariableQuery, enabled = true) =>
  useQuery({ queryKey: sk.variables(q), queryFn: () => studioApi.variables(q), enabled, staleTime: 10 * 60_000, placeholderData: keepPreviousData })

export const useStudioRuns = () => useQuery({ queryKey: sk.runs(), queryFn: () => studioApi.runs(), staleTime: 15_000 })
export const useStudioStudies = () => useQuery({ queryKey: sk.studies(), queryFn: () => studioApi.studies(), staleTime: 15_000 })

/** Polls every 1 s while the run is queued / running (live ETA, preview); stops once terminal. */
export const useStudioRun = (runId: string | null) =>
  useQuery({
    queryKey: sk.run(runId ?? ''),
    queryFn: () => studioApi.run(runId as string),
    enabled: !!runId,
    staleTime: Infinity,
    refetchInterval: (q) => {
      const s = q.state.data?.status.status
      return s && !isTerminal(s) ? 1000 : false
    },
  })

/** S1.2: learned estimate of the draft (re-asked when the spec changes; errors = invalid draft, shown elsewhere). */
export const useStudioEstimate = (study: CStudySpec | null) => {
  const key = study ? JSON.stringify(study) : ''
  return useQuery({
    queryKey: sk.estimate(key),
    queryFn: () => studioApi.estimate(study as CStudySpec),
    enabled: !!study,
    staleTime: 20_000,
    retry: false,
    placeholderData: keepPreviousData,
  })
}

export const useStudioLane = () => useQuery({ queryKey: sk.lane(), queryFn: () => studioApi.lane(), staleTime: 10_000, refetchInterval: 15_000 })

/** v1.50: compare runs (metrics, coefficients and out-of-fold tests vs the benchmark run). */
export const useStudioCompare = (ids: string[], benchmark: string | null) =>
  useQuery({ queryKey: sk.compare(ids, benchmark), queryFn: () => studioApi.compare(ids, benchmark), staleTime: 60_000, placeholderData: keepPreviousData })

/* ── v1.50 Model Registry ── */
export const useStudioRegistry = () => useQuery({ queryKey: sk.registry(), queryFn: () => studioApi.registry(), staleTime: 15_000 })
export const useStudioModel = (id: string | null) => useQuery({ queryKey: sk.model(id ?? ''), queryFn: () => studioApi.model(id as string), enabled: !!id, staleTime: 15_000 })

const invalidateRegistry = (qc: ReturnType<typeof useQueryClient>) => void qc.invalidateQueries({ queryKey: sk.registry() })

export const usePromoteRun = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (a: { runId: string; body: CPromoteRequest }) => studioApi.promote(a.runId, a.body), onSuccess: () => invalidateRegistry(qc) })
}

export const useUpdateModel = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (a: { modelId: string; patch: CModelUpdate }) => studioApi.updateModel(a.modelId, a.patch),
    // promoting to production demotes the other versions: refresh the whole registry
    onSuccess: () => invalidateRegistry(qc),
  })
}

export const useDeleteModel = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (modelId: string) => studioApi.deleteModel(modelId), onSuccess: () => invalidateRegistry(qc) })
}

export const useMonitorModel = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (a: { modelId: string; runId?: string | null; alert?: boolean }) => studioApi.monitorModel(a.modelId, a), onSuccess: () => invalidateRegistry(qc) })
}

export const useGuideSuggest = () => useMutation({ mutationFn: (q: CGuideQuestion) => studioApi.guideSuggest(q) })

export const useStudioPeerSets =(enabled = true) => useQuery({ queryKey: sk.peerSets(), queryFn: () => studioApi.peerSets(), staleTime: 60_000, enabled, retry: false })
