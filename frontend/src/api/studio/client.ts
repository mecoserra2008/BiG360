import type { components } from '../generated/schema'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import {
  adaptCatalog,
  adaptCompare,
  adaptEstimate,
  adaptLane,
  adaptMethod,
  adaptPreset,
  adaptPreview,
  adaptRun,
  adaptRunStatus,
  adaptStudy,
  adaptVariable,
} from './adapters'
import { adaptGuide, adaptLint, adaptModel, adaptMonitor } from './adaptersS3'
import type {
  CGuideQuestion,
  CModelUpdate,
  CPromoteRequest,
  GuideSuggestionView,
  LintView,
  StudioModelMonitorView,
  StudioModelView,
  CDatasetSpec,
  CLeaderboardRequest,
  StudioPeerSetView,
  CStudySpec,
  RunEstimateView,
  StudioLaneView,
  StudioCatalogView,
  StudioCompareView,
  StudioMethodView,
  StudioPresetView,
  StudioPreviewView,
  StudioRunStatusView,
  StudioRunView,
  StudioStudyView,
  StudioVariableView,
} from './types'

type S = components['schemas']

/** Model Studio has no mock implementation: it needs the backend's dataset engine. */
function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/studio`, 'operation Model Studio needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

const enc = encodeURIComponent

export interface VariableQuery {
  q?: string
  source?: string
  entityType?: 'security' | 'issuer' | 'macro'
  frequency?: string
  limit?: number
  offset?: number
}

export const studioApi = {
  async catalog(): Promise<StudioCatalogView> {
    live()
    return adaptCatalog(await http.get<S['StudioSourceCatalog']>('/studio/catalog/datasets'))
  },
  async variables(q: VariableQuery): Promise<{ total: number; items: StudioVariableView[] }> {
    live()
    const page = await http.get<S['StudioVariablePage']>('/studio/catalog/variables', {
      q: q.q || undefined,
      source: q.source || undefined,
      entity_type: q.entityType,
      frequency: q.frequency || undefined,
      limit: q.limit ?? 60,
      offset: q.offset ?? 0,
    })
    return { total: page.total, items: page.items.map(adaptVariable) }
  },
  async methods(): Promise<StudioMethodView[]> {
    live()
    return (await http.get<S['StudioMethod'][]>('/studio/catalog/methods')).map(adaptMethod)
  },
  async presets(): Promise<StudioPresetView[]> {
    live()
    return (await http.get<S['StudioPreset'][]>('/studio/presets')).map(adaptPreset)
  },
  async preview(dataset: CDatasetSpec, n = 50): Promise<StudioPreviewView> {
    live()
    return adaptPreview(await http.post<S['StudioDatasetPreview']>('/studio/datasets/preview', { dataset, n }))
  },
  /** mode auto: inline when cheap (200, finished), else queued on the worker (202) — poll `run()`. */
  async submit(study: CStudySpec, opts: { mode?: 'auto' | 'sync' | 'async'; studyId?: string | null } = {}): Promise<StudioRunView> {
    live()
    return adaptRun(await http.post<S['StudioRun']>('/studio/runs', { study, mode: opts.mode ?? 'auto', study_id: opts.studyId ?? null }))
  },
  /** S1.2: learned estimate (path, queue, overhead, build, fit, heavy extras with cost) before submitting. */
  async estimate(study: CStudySpec, mode: 'auto' | 'sync' | 'async' = 'auto'): Promise<RunEstimateView> {
    live()
    return adaptEstimate(await http.post<S['StudioRunEstimate']>('/studio/estimate', { study, mode }))
  },
  /** S1.1: warm studio lane status + estimate accuracy diagnostics. */
  async lane(): Promise<StudioLaneView> {
    live()
    return adaptLane(await http.get<S['StudioLaneStatus']>('/studio/lane'))
  },
  async run(runId: string): Promise<StudioRunView> {
    live()
    return adaptRun(await http.get<S['StudioRun']>(`/studio/runs/${enc(runId)}`))
  },
  async cancel(runId: string): Promise<StudioRunStatusView> {
    live()
    return adaptRunStatus(await http.delete<S['StudioRunStatus']>(`/studio/runs/${enc(runId)}`))
  },
  async runs(studyId?: string, limit = 100): Promise<StudioRunStatusView[]> {
    live()
    return (await http.get<S['StudioRunStatus'][]>('/studio/runs', { study_id: studyId, limit })).map(adaptRunStatus)
  },
  async exportPython(runId: string): Promise<string> {
    live()
    return http.get<string>(`/studio/runs/${enc(runId)}/export.py`)
  },
  /** v1.50: benchmarkRunId → out-of-fold DM / Clark-West tests and equity curves vs that run (default: the first). */
  async compare(runIds: string[], benchmarkRunId?: string | null): Promise<StudioCompareView> {
    live()
    return adaptCompare(await http.post<S['StudioCompare']>('/studio/compare', { run_ids: runIds, ...(benchmarkRunId ? { benchmark_run_id: benchmarkRunId } : {}) }))
  },
  /** v1.50 guided builder: structured question → suggested StudySpec + method rationale + pre-run lint. */
  async guideSuggest(q: CGuideQuestion): Promise<GuideSuggestionView> {
    live()
    return adaptGuide(await http.post<S['StudioGuideSuggestion']>('/studio/guide/suggest', q))
  },
  /** v1.50: look-ahead / leakage / survivorship / tiny-sample checks before a run. */
  async lint(study: CStudySpec): Promise<LintView> {
    live()
    return adaptLint(await http.post<S['StudioLintResult']>('/studio/lint', { study }))
  },
  /* ── v1.50 Model Registry ── */
  async promote(runId: string, body: CPromoteRequest): Promise<StudioModelView> {
    live()
    return adaptModel(await http.post<S['StudioModel']>(`/studio/runs/${enc(runId)}/promote`, body))
  },
  async registry(name?: string): Promise<StudioModelView[]> {
    live()
    return (await http.get<S['StudioModel'][]>('/studio/registry', { name: name || undefined })).map(adaptModel)
  },
  async model(modelId: string): Promise<StudioModelView> {
    live()
    return adaptModel(await http.get<S['StudioModel']>(`/studio/registry/${enc(modelId)}`))
  },
  async updateModel(modelId: string, patch: CModelUpdate): Promise<StudioModelView> {
    live()
    return adaptModel(await http.patch<S['StudioModel']>(`/studio/registry/${enc(modelId)}`, patch))
  },
  async deleteModel(modelId: string): Promise<void> {
    live()
    await http.del(`/studio/registry/${enc(modelId)}`)
  },
  async monitorModel(modelId: string, body: { runId?: string | null; alert?: boolean } = {}): Promise<StudioModelMonitorView> {
    live()
    return adaptMonitor(await http.post<S['StudioModelMonitor']>(`/studio/registry/${enc(modelId)}/monitor`, { run_id: body.runId ?? null, alert: body.alert ?? true }))
  },
  async studies(): Promise<StudioStudyView[]> {
    live()
    return (await http.get<S['StudioStudy'][]>('/studio/studies')).map(adaptStudy)
  },
  async study(studyId: string): Promise<{ study: StudioStudyView; runs: StudioRunStatusView[] }> {
    live()
    const d = await http.get<S['StudioStudyDetail']>(`/studio/studies/${enc(studyId)}`)
    return { study: adaptStudy(d.study), runs: d.runs.map(adaptRunStatus) }
  },
  async createStudy(body: { name: string; description?: string | null; study: CStudySpec; tags?: string[]; presetId?: string | null }): Promise<StudioStudyView> {
    live()
    return adaptStudy(
      await http.post<S['StudioStudy']>('/studio/studies', { name: body.name, description: body.description ?? null, study: body.study, tags: body.tags ?? [], preset_id: body.presetId ?? null }),
    )
  },
  async updateStudy(
    studyId: string,
    patch: { name?: string; description?: string | null; study?: CStudySpec; tags?: string[]; schedule?: 'none' | 'weekly' | 'monthly'; drift_threshold?: number },
  ): Promise<StudioStudyView> {
    live()
    return adaptStudy(await http.patch<S['StudioStudy']>(`/studio/studies/${enc(studyId)}`, patch))
  },
  /** Model leaderboard on identical CV splits (POST /studio/leaderboard → run, sync or queued). */
  async leaderboard(body: CLeaderboardRequest): Promise<StudioRunView> {
    live()
    return adaptRun(await http.post<S['StudioRun']>('/studio/leaderboard', body))
  },
  async createPeerSet(runId: string, body: { name: string; description?: string | null }): Promise<StudioPeerSetView> {
    live()
    return http.post<StudioPeerSetView>(`/studio/runs/${enc(runId)}/peer-set`, { name: body.name, description: body.description ?? null })
  },
  async peerSets(): Promise<StudioPeerSetView[]> {
    live()
    return http.get<StudioPeerSetView[]>('/studio/peer-sets')
  },
  async peerSet(id: string): Promise<StudioPeerSetView> {
    live()
    return http.get<StudioPeerSetView>(`/studio/peer-sets/${enc(id)}`)
  },
  async deletePeerSet(id: string): Promise<void> {
    live()
    await http.del(`/studio/peer-sets/${enc(id)}`)
  },
  async deleteStudy(studyId: string): Promise<void> {
    live()
    await http.del(`/studio/studies/${enc(studyId)}`)
  },
}
