/**
 * Model Studio view-model types (camelCase) for /api/v1/studio (contract v1.5).
 * Result internals (plots, tables, forecast, clusters, evaluation) are render data and keep the
 * contract shape — see `StudioResultView`.
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type CStudySpec = S['StudioStudySpec']
export type CDatasetSpec = S['StudioDatasetSpec']
export type CVariableSpec = S['StudioVariableSpec']
export type CTransform = S['StudioTransform']
export type CEntityFilter = S['StudioEntityFilter']
export type StudioPlot = S['StudioPlot']
export type StudioSeries = S['StudioSeries']
export type StudioTable = S['StudioTable']
export type StudioForecast = S['StudioForecast']
export type StudioClusters = S['StudioClusters']
export type StudioEvaluation = S['StudioEvaluation']
export type StudioCoefficient = S['StudioCoefficient']
export type StudioWarning = S['StudioWarning']
export type StudioTreatment = {
  mode?: 'entities' | 'attribute' | 'column' | 'events'
  entities?: string[]
  attribute?: 'sector_id' | 'region' | 'country' | null
  values?: string[]
  column?: string | null
  date?: string | null
  events?: { entity: string; date: string }[]
}

export type TransformOp = CTransform['op']
export type DatasetKind = CDatasetSpec['kind']
export type Frequency = CDatasetSpec['frequency']
export type RunState = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

/* ── catalog ── */

export interface StudioSourceView {
  source: string
  table: string
  title: string
  entityType: S['StudioVariable']['entity_type']
  nativeFrequency: string
  rows: number
  nEntities: number
  nVariables: number | null
  firstDate: string | null
  lastDate: string | null
}

export interface StudioCatalogView {
  snapshotId: string
  sources: StudioSourceView[]
  /** op → description */
  transforms: Record<string, string>
  universes: string[]
}

export interface StudioVariableView {
  key: string
  source: string
  field: string
  table: string
  title: string
  country: string | null
  frequency: string
  units: string | null
  entityType: S['StudioVariable']['entity_type']
  nObs: number
  nEntities: number | null
  firstDate: string | null
  lastDate: string | null
  pit: boolean
}

/** One form control derived from a method's params JSON schema. */
export interface ParamField {
  key: string
  title: string
  description: string | null
  kind:
    | 'object'
    | 'candidates'
    | 'boolean'
    | 'integer'
    | 'number'
    | 'string'
    | 'date'
    | 'enum'
    | 'int_tuple'
    | 'number_list'
    | 'string_list'
    | 'int_map'
    | 'treatment'
    | 'events'
    | 'json'
  options: string[]
  /** raw enum values (numbers stay numbers, e.g. det_order -1 / 0 / 1) */
  enumValues: unknown[]
  min: number | null
  max: number | null
  exclusiveMin: number | null
  exclusiveMax: number | null
  nullable: boolean
  defaultValue: unknown
  /** int_tuple length */
  length: number | null
  /** listed in the schema's `required` (no default) */
  required: boolean
  /** kind 'object': nested fields (e.g. cv: folds / scheme / purge / embargo) */
  children: ParamField[]
}

export interface StudioMethodView {
  id: string
  family: S['StudioMethod']['family']
  title: string
  description: string
  shapes: DatasetKind[]
  needsY: boolean
  minX: number
  maxX: number | null
  usesWeights: boolean
  outputs: string[]
  diagnostics: string[]
  fields: ParamField[]
}

export interface StudioPresetView {
  id: string
  title: string
  description: string
  method: string
  tags: string[]
  study: CStudySpec
  /** v1.50 recipes: question group (factors, events, macro, risk, forecasting …) */
  question: string | null
  methodNote: string | null
  expectedOutputs: string[]
  /** measured end-to-end runtime on the real lake */
  runtimeS: number | null
  validated: string | null
}

/* ── builder draft (UI state ↔ StudySpec) ── */

/** endog / instrument: IV roles, mapped to params.endog / params.instruments (not part of x) */
export type VariableRole = 'y' | 'x' | 'weights' | 'feature' | 'endog' | 'instrument' | 'none'

export interface DraftVariable {
  uid: string
  name: string
  source: string
  field: string
  title: string | null
  role: VariableRole
  agg: CVariableSpec['agg'] | null
  entityAgg: CVariableSpec['entity_agg'] | null
  entities: CEntityFilter | null
  preTransforms: CTransform[]
  transforms: CTransform[]
  /** fields of the variable spec the builder does not edit (options, max_stale_days …) — preserved verbatim */
  extra: Record<string, unknown>
}

export interface StudyDraft {
  name: string
  kind: DatasetKind
  frequency: Frequency
  start: string | null
  end: string | null
  entities: CEntityFilter
  entityLevel: CDatasetSpec['entity_level']
  macroAlignment: CDatasetSpec['macro_alignment']
  variables: DraftVariable[]
  method: string
  params: Record<string, unknown>
  seed: number
  timeoutS: number
  /** dataset-spec fields not edited by the builder (missing, dummies, lookback, max_rows …) */
  datasetExtra: Record<string, unknown>
}

/* ── preview ── */

export interface StudioPreviewView {
  datasetKey: string
  snapshotId: string
  cached: boolean
  kind: string
  frequency: string
  start: string
  end: string
  nRows: number
  nEntities: number
  nPeriods: number
  columns: { name: string; dtype: string }[]
  rows: Record<string, unknown>[]
  stats: S['StudioColumnStats'][]
  coverage: { column: string; nNonNull: number; pctNonNull: number; firstDate: string | null; lastDate: string | null; nEntities: number | null; usesFuture: boolean }[]
  warnings: string[]
  buildSeconds: number | null
}

/* ── runs & results ── */

export interface StudioRunStatusView {
  runId: string
  studyId: string | null
  name: string | null
  title: string | null
  method: string
  status: RunState
  mode: 'sync' | 'async'
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
  durationS: number | null
  estimateS: number | null
  error: string | null
  errorCode: string | null
  specHash: string | null
  nObs: number | null
  warningsCount: number | null
  metrics: Record<string, number | string | null>
  /** S1 (v1.48): where it runs (sync inline / warm lane / worker fallback), live stage, queue and ETA. */
  lane: 'sync' | 'lane' | 'worker' | null
  stage: string | null
  etaS: number | null
  queue: RunQueueView | null
  estimate: RunEstimateView | null
  preview: RunPreviewInfo | null
}

export interface RunQueueView {
  position: number | null
  ahead: number
  blockedBy: string | null
  queueS: number | null
}

export interface RunExtraView {
  name: string
  label: string
  enabled: boolean
  costS: number
}

/** Learned estimate breakdown (S1.2): total = queue + overhead + build + fit (+ post). */
export interface RunEstimateView {
  totalS: number
  runS: number
  queueS: number
  overheadS: number
  buildS: number
  fitS: number
  path: 'sync' | 'lane' | 'worker'
  model: 'learned' | 'global' | 'prior'
  nHistory: number
  extras: RunExtraView[]
}

export interface RunPreviewInfo {
  status: 'completed' | 'failed'
  fraction: number | null
  by: string | null
  nRows: number | null
  nRowsFull: number | null
  durationS: number | null
  error: string | null
  skipped: string[]
}

export interface StudioLaneView {
  enabled: boolean
  alive: boolean
  pool: number | null
  priority: string | null
  heavyJobs: string[]
  running: number
  queued: number
  blockedBy: string | null
  accuracy: { studio: LaneAccuracy; backtest: LaneAccuracy }
}

export interface LaneAccuracy {
  n: number
  medianRatio: number | null
  within30: number | null
}

export type TestVerdict = 'pass' | 'fail' | 'significant' | 'not_significant' | 'info'

export interface StudioTestView extends Omit<S['StudioTest'], 'statistic' | 'p_value' | 'df'> {
  statistic: number | null
  pValue: number | null
  /** number, or [df1, df2] for F tests */
  df: number | (number | null)[] | null
  verdict: TestVerdict
}

export interface StudioResultView {
  method: string
  family: string
  title: string
  summary: Record<string, unknown>
  fitStats: Record<string, unknown>
  coefficients: StudioCoefficient[]
  tests: StudioTestView[]
  tables: StudioTable[]
  forecast: StudioForecast | null
  clusters: StudioClusters | null
  evaluation: StudioEvaluation | null
  plots: Record<string, StudioPlot>
  warnings: StudioWarning[]
  metrics: Record<string, unknown>
}

export interface StudioRunView {
  status: StudioRunStatusView
  study: CStudySpec | null
  dataset: (Omit<S['StudioDatasetInfo'], 'coverage'> & { coverage: StudioPreviewView['coverage'] }) | null
  result: StudioResultView | null
  reproducibility: S['StudioRunEnvelope']['reproducibility'] | null
  /** S1.3: sample result while the full run is still running. */
  preview: { info: RunPreviewInfo; result: StudioResultView | null } | null
}

/* ── studies & compare ── */

export interface StudioStudyView {
  studyId: string
  name: string
  description: string | null
  method: string
  tags: string[]
  presetId: string | null
  lastRunId: string | null
  /** v1.7 scheduled re-runs (studio_scheduled job) */
  schedule: 'none' | 'weekly' | 'monthly'
  driftThreshold: number | null
  lastScheduledAt: string | null
  alertId: string | null
  study: CStudySpec
  createdAt: string
  updatedAt: string
}

export type StudioPeerSetView = S['StudioPeerSet']
export type CLeaderboardRequest = S['StudioLeaderboardRequest']
export type LeaderboardCandidate = CLeaderboardRequest['candidates'][number]

export interface StudioCompareView {
  runIds: string[]
  runs: StudioRunStatusView[]
  metrics: { metric: string; values: Record<string, number | null> }[]
  coefficients: { term: string; values: Record<string, { estimate: number | null; stdError: number | null; pValue: number | null } | null> }[]
  /** v1.50: out-of-fold forecast comparison vs a benchmark run (null on older backends) */
  oos: StudioCompareOosView | null
}

/* ── S3 (v1.50): out-of-fold comparison ── */

export interface OosTestView {
  runId: string
  benchmarkRunId: string
  nRows: number
  nDates: number | null
  mseModel: number | null
  mseBenchmark: number | null
  /** > 0: the run is more accurate */
  dmStat: number | null
  dmP: number | null
  nested: boolean
  cwStat: number | null
  cwP: number | null
  verdict: string | null
  note: string | null
}

export interface OosEquityView {
  runId: string
  kind: 'top_minus_bottom_quintile' | 'sign_of_prediction'
  dates: string[]
  cumReturn: (number | null)[]
  sharpe: number | null
  meanIc: number | null
  nPeriods: number | null
}

export interface StudioCompareOosView {
  benchmarkRunId: string | null
  tests: OosTestView[]
  equity: OosEquityView[]
  skipped: { runId: string; reason: string }[]
}

/* ── S3 (v1.50): guided builder & pre-run lint ── */

export type CGuideQuestion = S['StudioGuideQuestion']
export type CGuideVar = S['StudioGuideVar']
export type GuideGoal = NonNullable<CGuideQuestion['goal']>
export type GuideOutcomeKind = NonNullable<CGuideQuestion['outcome_kind']>
export type LintSeverity = 'error' | 'warning' | 'info'

export interface LintIssueView {
  code: string
  severity: LintSeverity
  message: string
  fix: string
  columns: string[]
}

export interface LintView {
  /** no error-severity issue */
  ok: boolean
  issues: LintIssueView[]
  estimatedRows: number | null
  nEntities: number | null
}

export interface GuideSuggestionView {
  study: CStudySpec
  method: string
  rationale: string[]
  alternatives: { method: string; why: string }[]
  lint: LintView
}

/* ── S3 (v1.50): Model Registry ── */

export type ModelStage = 'candidate' | 'production' | 'archived'
export type MonitorSchedule = 'none' | 'weekly' | 'monthly'

export interface ModelMetricView {
  kind: string | null
  value: number | null
  baseline: number | null
}

export interface ModelLastMonitorView {
  drift: boolean | null
  breaches: string[]
  metric: ModelMetricView | null
  predictionPsi: number | null
}

export interface StudioModelView {
  modelId: string
  name: string
  version: number
  runId: string
  method: string
  spec: Record<string, unknown> | null
  specHash: string
  snapshotId: string | null
  datasetKey: string | null
  owner: string
  description: string | null
  stage: ModelStage
  metrics: Record<string, number | null>
  monitor: MonitorSchedule
  psiThreshold: number
  metricFloor: number | null
  alertId: string | null
  lastMonitor: ModelLastMonitorView | null
  /** detail only: the full stored result of the last monitor run */
  lastMonitorDetail: StudioModelMonitorView | null
  lastMonitoredAt: string | null
  createdAt: string | null
  updatedAt: string | null
  /** detail only: every version of this name (list rows) */
  versions: StudioModelView[]
}

export type CPromoteRequest = S['StudioPromoteRequest']
export type CModelUpdate = S['StudioModelUpdate']

export type PsiStatus = 'stable' | 'watch' | 'drift'

export interface ExpandingPointView {
  date: string
  ic: number | null
  expandingIc: number | null
  expandingIcT: number | null
  expandingR2Oos: number | null
  expandingRmse: number | null
}

export interface StudioModelMonitorView {
  modelId: string
  runId: string
  currentWindow: { start: string | null; end: string | null; afterBaselineEnd: boolean | null } | null
  featurePsi: { feature: string; psi: number | null; status: PsiStatus | null; nCurrent: number | null }[]
  predictionPsi: number | null
  metric: ModelMetricView | null
  expanding: ExpandingPointView[]
  breaches: string[]
  drift: boolean
  psiThreshold: number | null
  metricFloor: number | null
}
